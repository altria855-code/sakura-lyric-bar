"""插件侧:启动、监控、驱动浮窗子进程(设计文档第 6.4 节的传输层)。

本类**只做传输**:标准输出上收到一行就 `protocol.decode` 一次、回调 `on_message`
一次,不解释消息语义、**不主动发任何消息** —— 首条 `hello` 由
`LyricBarRuntime.start()` 发送(Task 13)。`bye` 在插件侧按「未知消息类型忽略」
处理(它只是浮窗退出前打的招呼)。

日志纪律(宿主日志窗口按字段收集):只写 `operation` / `reason_code`
(`error_type` 只在有异常时补上),**不写台词与用户输入**;子进程 stderr 只转长度
与前 200 字符,不整行抄。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import protocol

try:  # 宿主公开 SDK:插件用它回收自己启动的子进程树(spec §2)
    from sakura_process import terminate_process_tree
except Exception:  # noqa: BLE001 — 不在宿主里(测试、命令行自查)时没有这个模块
    terminate_process_tree = None

# CreateProcess 的 CREATE_NO_WINDOW 标志(0x08000000):浮窗是 pythonw,本来就没有
# 控制台;兜底到 python.exe 时靠它别闪一个黑框。win32.py 里没有绑定这个常量。
CREATE_NO_WINDOW = 0x08000000

READ_CHUNK_BYTES = 65536  # 每次 os.read 的上限
PARSE_FAILURE_LIMIT = 3  # 连续这么多行解不出来就停止读(浮窗侧也以 3 条为界)
STOP_TIMEOUT_SECONDS = 2.0  # 请它退出后最多等这么久
BYE_WRITE_TIMEOUT_SECONDS = 0.2  # 收尾时取写锁最多等这么久,取不到就跳过 bye(见 _write_bye)
STDIN_CLOSE_TIMEOUT_SECONDS = 0.5  # 关 stdin 最多等这么久:正常微秒级,卡住就交给进程树清理
STDERR_PREVIEW_CHARS = 200  # stderr 只留前 200 字符进日志
STDERR_BUFFER_LIMIT = 65536  # stderr 一直没有换行时,不无限攒


def _default_python() -> str:
    """宿主自带的解释器:优先同目录的 pythonw(无控制台窗口),没有就用当前解释器。"""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate) if candidate.exists() else str(sys.executable)


def _fileno(stream: object) -> int | None:
    """取裸 fd;没有(已经是 None / 已关闭)时给 None,调用方按「读不了」处理。"""
    try:
        return int(stream.fileno())  # type: ignore[union-attr]
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _close_stream(stream: object) -> None:
    """读完把自己这头的管道关掉:谁读谁关,别把 unclosed file 留给 GC 报 ResourceWarning。"""
    close = getattr(stream, "close", None)
    if close is None:
        return
    try:
        close()
    except (OSError, ValueError):
        pass  # 已经关了 / 已经断了


class OverlayLink:
    """浮窗进程的一条链路:`start()` 拉起、`send()` 喂消息、`stop()` 收干净。"""

    def __init__(
        self,
        plugin_dir: str,
        log: Any,
        on_message: Callable[[dict[str, Any]], None],
        script: str | None = None,
        python_executable: str | None = None,
    ) -> None:
        self._plugin_dir = str(plugin_dir)
        self._log = log
        self._on_message = on_message
        self._script = str(script) if script else os.path.join(self._plugin_dir, "overlay.py")
        self._python = str(python_executable) if python_executable else _default_python()

        self._process: subprocess.Popen | None = None
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()  # 写管道要成行:两个发送方不能交错字节
        self._stop_requested = False
        self._stopping = False
        self._failures = 0

        # 对外状态:读线程会改(running),失败原因留给调用方看(last_error)
        self.running = False
        self.last_error = ""

    # --- 生命周期 ---

    def start(self) -> bool:
        """拉起浮窗。已在运行、脚本不在、起不来都返回 False(原因见 `last_error`)。"""
        if self.running:
            return False
        if self._process is not None:
            self.stop()  # 上一轮的残留(已退出的话只是把句柄回收掉)
        if not os.path.isfile(self._script):
            self.last_error = "OVERLAY_SCRIPT_MISSING"
            self._log.error(
                "浮窗脚本不存在",
                fields={"operation": "overlay_start", "reason_code": "OVERLAY_SCRIPT_MISSING"},
            )
            return False

        try:
            process = subprocess.Popen(
                [self._python, self._script, "--run"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self._plugin_dir,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as error:
            self.last_error = "OVERLAY_START_FAILED"
            self._log.error(
                "浮窗启动失败",
                fields={"operation": "overlay_start", "reason_code": "OVERLAY_START_FAILED",
                        "error_type": type(error).__name__},
            )
            return False

        with self._state_lock:
            self._process = process
            self.running = True
            self._stop_requested = False
            self._failures = 0
        self.last_error = ""

        # 两个守护线程:进程退出时不必等它们从阻塞的 read 里醒过来
        self._read_in_background(self._read_stdout, (process, process.stdout), "overlay-stdout")
        self._read_in_background(self._read_stderr, (process.stderr,), "overlay-stderr")
        return True

    def send(self, message: dict[str, Any]) -> bool:
        """喂一条消息给浮窗;没有子进程 / 写失败都返回 False,不抛异常。"""
        with self._state_lock:
            process = self._process
            if not self.running or process is None:
                return False
        stream = process.stdin
        if stream is None:
            return False
        data = protocol.encode(message)
        try:
            with self._write_lock:
                stream.write(data)
                stream.flush()
        except (OSError, ValueError) as error:
            # 管道断了(浮窗已死):别让调用方崩在写管道上。
            # 但「写失败」不等于「当前这一轮死了」——重启(stop+start)横穿时,慢发送写的
            # 可能是已经关掉的旧管道,那种情况绝不能把健康的新一轮打成未运行(与读路径
            # 的 `_mark_not_running` 同一把尺子);只有确实是当前这一轮才记告警。
            if self._mark_not_running(process):
                self._log.warning(
                    "写给浮窗失败",
                    fields={"operation": "overlay_send", "reason_code": "OVERLAY_WRITE_FAILED",
                            "error_type": type(error).__name__},
                )
            return False
        return True

    def stop(self) -> None:
        """请浮窗退出并收干净。幂等 —— 重复调用、没 start 过都安全。

        `_process` 要等到收尾做完才清:收尾这三步(bye / 关 stdin / 等或收)全都要用它,
        `send()` 的身份校验也拿它比对。这中间的重复调用靠 `_stopping` 挡住,而不是靠清空 `_process`。
        """
        with self._state_lock:
            process = self._process
            if process is None:
                self.running = False  # 没 start 过 / 已经收干净了
                return
            if self._stopping:
                return  # 另一次 stop() 正在收:等它收完(它会置 running=False,别在这里抢着置)
            self._stopping = True
            self._stop_requested = True  # 是我们让它退的:EOF 那边不必再记一条退出 warning
        try:
            self._stop_process(process)
        finally:
            with self._state_lock:
                self._stopping = False
                self._process = None  # 收干净了才松手:这中间 start() 也不可能新建(见 running)
                self.running = False

    # --- 内部:收尾 ---

    def _stop_process(self, process: subprocess.Popen) -> None:
        """一个进程的收尾:先礼(bye)、后兵(关 stdin),到点还不走就硬收。"""
        alive = process.poll() is None
        if alive:
            self._write_bye(process)  # 先礼:请它自己退(有界,见该方法)
        self._close_stdin_bounded(process)  # 后兵 / 收尾:关 stdin —— 浮窗的 EOF 生命线
        if not alive:
            try:
                process.wait()  # 已经退了:立刻回收退出码与句柄
            except OSError:
                pass
            return
        self._wait_or_terminate(process)

    def _write_bye(self, process: subprocess.Popen) -> None:
        """礼貌的 bye:**带超时地**取写锁,取不到就跳过。

        为什么不能直接 `send()`:发送线程可能正阻塞在满管道上(Windows 匿名管道只有几 KB),
        并占着 `_write_lock` —— `stop()` 要是普通地等这把锁,后面的关 stdin、2 秒超时、
        进程树清理一步都到不了(评审实测 8 秒仍未返回),插件卸载会被无限期挂住。
        拿不到锁就少说一句告别:浮窗照样会因为下面 stdin 的 EOF 退出,再不行还有 2 秒后的
        进程树清理兜底。顺序不变:这一次写永远发生在 `_close_stdin_bounded` 之前。
        """
        stream = process.stdin
        if stream is None:
            return
        if not self._write_lock.acquire(timeout=BYE_WRITE_TIMEOUT_SECONDS):
            self._log.debug(
                "浮窗忙,跳过 bye",
                fields={"operation": "overlay_stop", "reason_code": "OVERLAY_BYE_SKIPPED"},
            )
            return
        try:
            stream.write(protocol.encode(protocol.bye_message()))
            stream.flush()
        except (OSError, ValueError):
            pass  # 写不进去就算了:关 stdin / 收进程树照样能让它退
        finally:
            self._write_lock.release()

    def _close_stdin_bounded(self, process: subprocess.Popen) -> None:
        """关 stdin(浮窗的 EOF 生命线),但**不许它把 stop() 挂住**。

        `stream.close()` 要等的是同一把缓冲锁:发送线程要是正卡在满管道的那次 `write()` 里
        (Windows 匿名管道只有几 KB),`close()` 就一直不返回 —— stop() 也就再也到不了
        2 秒超时与进程树清理(实测栈:stopper 停在 `_close_stdin` → `stream.close()`)。
        正常情况这里是微秒级;真卡住了就往下走,交给进程树清理收场:子进程一死,那次写会失败、
        锁会松开,这个关流的小线程自己就醒了(daemon,不拦解释器退出,也不碰任何链接状态)。
        """
        closer = threading.Thread(
            target=self._close_stdin, args=(process,), name="overlay-stdin-close", daemon=True
        )
        closer.start()
        closer.join(STDIN_CLOSE_TIMEOUT_SECONDS)

    def _close_stdin(self, process: subprocess.Popen) -> None:
        stream = process.stdin
        if stream is None:
            return
        try:
            stream.close()
        except OSError:
            pass  # 已经断了

    def _wait_or_terminate(self, process: subprocess.Popen) -> None:
        try:
            process.wait(timeout=STOP_TIMEOUT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass  # 到点还不走:下面的收进程树
        except OSError:
            return  # 等不了(句柄没了):交给 GC
        self._terminate(process)

    def _terminate(self, process: subprocess.Popen) -> None:
        """硬收。优先用宿主的进程树清理(SDK 公开接口),没有就退化成单进程 kill。"""
        if terminate_process_tree is not None:
            try:
                terminate_process_tree(process, timeout=STOP_TIMEOUT_SECONDS)
                return
            except Exception as error:  # noqa: BLE001 — 清理路径绝不能再抛
                self._log.warning(
                    "浮窗进程树清理失败",
                    fields={"operation": "overlay_stop", "reason_code": "OVERLAY_KILL_FAILED",
                            "error_type": type(error).__name__},
                )
        try:
            process.kill()
            process.wait(timeout=STOP_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as error:
            self._log.warning(
                "浮窗进程无法终止",
                fields={"operation": "overlay_stop", "reason_code": "OVERLAY_KILL_FAILED",
                        "error_type": type(error).__name__},
            )

    # --- 内部:读管道 ---

    def _read_in_background(self, target: Callable[..., None], args: tuple[Any, ...],
                            name: str) -> None:
        threading.Thread(target=target, args=args, name=name, daemon=True).start()

    def _read_stdout(self, process: subprocess.Popen, stream: object) -> None:
        """逐行读浮窗 stdout,**裸 fd 自己缓冲切行**。

        为什么不用 `stream.readline()` / `for line in stream`:守护线程会持着
        BufferedReader 的锁阻塞在那里,本进程收尾要拿同一把锁去关这个流,撞上就是
        `_enter_buffered_busy`(0xC0000005)—— 与浮窗侧修掉的是同一个坑,这边保持
        同一种写法。那句实测结论只对本进程自己的标准输入成立,不是「读管道必崩」。

        stdout 的 EOF 就是浮窗的死讯:管道那头没人了,`running` 跟着置 False。
        状态只在「还是当前这一轮进程」时才改:上一轮的读线程可能比新一轮的
        `start()` 醒得晚,别让它把新一轮的 running 改掉。
        """
        handle = _fileno(stream)
        if handle is None:
            self._mark_exited(process)
            return
        try:
            pending = b""
            while True:
                try:
                    chunk = os.read(handle, READ_CHUNK_BYTES)
                except OSError:
                    break  # 管道断了,按 EOF 处理
                if not chunk:
                    break  # EOF
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if not self._deliver(line):
                        # 连续解析失败:不读了(浮窗那边也会因协议错误自己退)。
                        # 这条通道已经废了,别再声称还活着 —— 但这不是「进程已退出」。
                        self._mark_not_running(process)
                        return
                if len(pending) > protocol.MAX_LINE_BYTES:
                    # 还没读到换行就已超长:按「解不出来的一行」处理,丢掉这段接着读
                    pending = b""
                    if not self._note_parse_failure():
                        self._mark_not_running(process)
                        return
        finally:
            _close_stream(stream)  # 读完了:管道这头由读的人关
        self._mark_exited(process)  # 走到这里 = EOF / 管道断:浮窗没了

    def _read_stderr(self, stream: object) -> None:
        """把浮窗的 stderr 转成宿主日志的一行 warning。

        只记长度与前 200 字符,不整行抄:stderr 上可能有 traceback 之类的长文本,
        既吵又可能带上不该进宿主日志的内容。stderr 的 EOF 不当作退出信号
        (浮窗的死活只看 stdout 那条通道)。
        """
        handle = _fileno(stream)
        if handle is None:
            return
        try:
            pending = b""
            while True:
                try:
                    chunk = os.read(handle, READ_CHUNK_BYTES)
                except OSError:
                    break
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    self._forward_stderr(line)
                if len(pending) > STDERR_BUFFER_LIMIT:
                    self._forward_stderr(pending)  # 一直没有换行:当成一行报掉
                    pending = b""
        finally:
            _close_stream(stream)

    def _deliver(self, raw: bytes) -> bool:
        """处理一行;返回 False 表示读线程该收工了。"""
        line = raw.rstrip(b"\r")
        if not line.strip():
            return True  # 空行不是消息,也不算解析失败
        try:
            message = protocol.decode(line)
        except protocol.ProtocolError:
            return self._note_parse_failure()
        self._failures = 0
        try:
            self._on_message(message)
        except Exception as error:  # noqa: BLE001 — 收方的事,别带走读线程
            self._log.error(
                "浮窗消息处理失败",
                fields={"operation": "overlay", "reason_code": "OVERLAY_HANDLER_FAILED",
                        "error_type": type(error).__name__},
            )
        return True

    def _note_parse_failure(self) -> bool:
        """记一次解析失败;返回 False 表示连续失败到上限,读线程该收工了。

        日志只留原因码,不写异常正文(`str(error)` 之类):协议消息文本的形状不该被
        日志依赖 —— 今天 `ProtocolError` 的消息里没有载荷,明天也未必。
        """
        self._failures += 1
        self._log.warning(
            "浮窗消息无法解析",
            fields={"operation": "overlay", "reason_code": "OVERLAY_PROTOCOL_INVALID"},
        )
        return self._failures < PARSE_FAILURE_LIMIT

    def _forward_stderr(self, raw: bytes) -> None:
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            return
        self._log.warning(
            f"浮窗 stderr({len(text)} 字符):{text[:STDERR_PREVIEW_CHARS]}",
            fields={"operation": "overlay", "reason_code": "OVERLAY_STDERR"},
        )

    def _mark_not_running(self, process: subprocess.Popen) -> bool:
        """置 `running=False`;返回 False 表示这次调用不算数。

        不算数的三种情形:这一轮已经翻篇了(`_process` 换了)、它本来就不在运行、
        或者**我们正在主动收尾**(`_stopping`:收尾途中的写失败 / EOF 都不是「浮窗死了」,
        没必要记一条告警挂到收尾动作头上 —— 收尾自己会把 `running` 置 False)。
        """
        with self._state_lock:
            if self._process is not process or not self.running or self._stopping:
                return False
            self.running = False
            return True

    def _mark_exited(self, process: subprocess.Popen) -> None:
        """stdout 到了 EOF:浮窗没了。只认「从运行到不运行」这一次跳变。"""
        if not self._mark_not_running(process):
            return  # 正常停止留下的余音 / 已经判过死:不重复记
        if self._stop_requested:
            return  # 是我们让它退的,不是意外
        self._log.warning(
            "浮窗进程已退出",
            fields={"operation": "overlay", "reason_code": "OVERLAY_EXITED"},
        )
