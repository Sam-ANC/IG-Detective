import sys
import getpass
import glob
import threading
import queue

from rich.console import Console
from src.cli.formatters import print_splash
from src.api.client import InstagramClient
from src.cli.shell import IGDetectiveShell
from src.api.auth import SessionManager

try:
    from playwright._impl._errors import Error as PlaywrightError
    from playwright._impl._errors import TimeoutError as PlaywrightTimeoutError
except ImportError:
    PlaywrightError = Exception
    PlaywrightTimeoutError = Exception

console = Console()


# =============================================================================
# PLAYWRIGHT WORKER THREAD
#
# sync_playwright creates a greenlet/fiber dispatcher bound to the OS thread
# where it was started. Any call to page.evaluate() or page.goto() from a
# different thread raises:
#     "cannot switch to a different thread (which has exited)"
#
# cmd2 + prompt_toolkit on Python 3.12+ calls asyncio.run() internally,
# which conflicts with Playwright's event loop when both share the same thread.
#
# Solution: InstagramClient lives PERMANENTLY in a dedicated worker thread.
# The shell sends lambdas via a job queue and blocks waiting for the result.
# The main thread runs only the cmd2 shell — zero Playwright, zero conflict.
# =============================================================================

class PlaywrightWorker:
    """Dedicated thread that owns the Playwright event loop for its entire lifetime."""

    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._client: InstagramClient | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        """Worker loop: executes every job in the same OS thread."""
        while True:
            job = self._q.get()
            if job is None:
                break
            fn, box = job
            try:
                box["result"] = fn()
            except Exception as e:
                box["error"] = e
            finally:
                box["done"].set()

    def _submit(self, fn, timeout: int = 120):
        box = {"done": threading.Event()}
        self._q.put((fn, box))
        if not box["done"].wait(timeout=timeout):
            raise RuntimeError(f"Playwright worker timed out after {timeout}s.")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def init_client(self, username=None):
        """Create InstagramClient inside the worker thread."""
        def _fn():
            self._client = InstagramClient(username=username)
        self._submit(_fn, timeout=120)

    def call(self, fn, timeout: int = 90):
        """Run fn(client) inside the worker thread and return the result."""
        return self._submit(lambda: fn(self._client), timeout=timeout)

    def stop(self):
        self._q.put(None)
        self._thread.join(timeout=10)


# =============================================================================
# THREAD-SAFE CLIENT PROXY
#
# Wraps InstagramClient so that shell.py / recon.py / modules call it normally
# (e.g. client.fetch_user_info("x")) while every call is transparently
# forwarded to the worker thread where Playwright lives.
#
# Key fix: callable detection and invocation happen in a SINGLE worker.call()
# round-trip, avoiding double-getattr and race conditions.
# =============================================================================

class ThreadSafeClientProxy:
    """
    Transparent proxy for InstagramClient.
    Attribute reads and method calls are all routed through the worker thread.
    """

    def __init__(self, worker: PlaywrightWorker):
        object.__setattr__(self, "_worker", worker)

    def __getattr__(self, name: str):
        worker: PlaywrightWorker = object.__getattribute__(self, "_worker")

        # Check if the attribute is callable in a single worker round-trip
        is_callable = worker.call(lambda c: callable(getattr(c, name)))

        if is_callable:
            def _method(*args, **kwargs):
                return worker.call(lambda c: getattr(c, name)(*args, **kwargs))
            return _method
        else:
            return worker.call(lambda c: getattr(c, name))

    def __setattr__(self, name: str, value):
        if name == "_worker":
            object.__setattr__(self, name, value)
        else:
            worker: PlaywrightWorker = object.__getattribute__(self, "_worker")
            worker.call(lambda c: setattr(c, name, value))


# =============================================================================
# SESSION VALIDATION
# =============================================================================

def validate_session(proxy: ThreadSafeClientProxy) -> tuple[bool, str]:
    is_auth = proxy.is_authenticated
    if not is_auth:
        return False, "No sessionid found. The cookie may have expired."
    return True, "Session valid and authenticated."


# =============================================================================
# AUTHENTICATION FLOWS
# =============================================================================

def _init_guest(worker: PlaywrightWorker) -> ThreadSafeClientProxy:
    worker.init_client(username=None)
    return ThreadSafeClientProxy(worker)


def _flow_login_password(worker: PlaywrightWorker) -> ThreadSafeClientProxy:
    username = input("  Instagram Username: ").strip()
    password = getpass.getpass("  Instagram Password: ").strip()
    console.print(f"[dim]  Authenticating as {username}...[/dim]")

    try:
        SessionManager.perform_login(username, password)
        console.print("[dim]  Session saved. Launching browser...[/dim]")
        worker.init_client(username=username)
        proxy = ThreadSafeClientProxy(worker)
        is_valid, msg = validate_session(proxy)
        color = "green" if is_valid else "yellow"
        icon = "✅" if is_valid else "⚠️ "
        console.print(f"[bold {color}]  {icon} {msg}[/bold {color}]")
        return proxy

    except ConnectionError:
        console.print("[bold red]  ❌ Network Error: cannot reach Instagram servers.[/bold red]")
    except PlaywrightTimeoutError:
        console.print("[bold red]  ❌ Playwright timeout.[/bold red]")
    except PlaywrightError as e:
        console.print(f"[bold red]  ❌ Headless browser error: {e}[/bold red]")
        console.print("[dim]     Try running: playwright install chromium[/dim]")
    except Exception as e:
        console.print(f"[bold red]  ❌ {e}[/bold red]")

    console.print("[dim]  Falling back to Guest mode...[/dim]")
    return _init_guest(worker)


def _flow_load_session(worker: PlaywrightWorker) -> ThreadSafeClientProxy:
    session_files = glob.glob("session-*") + glob.glob("sessions/session-*")
    if session_files:
        console.print("\n[dim]  Saved sessions found:[/dim]")
        for i, f in enumerate(session_files, 1):
            console.print(f"  [cyan]{i}.[/cyan] {f}")
        console.print()

    username = input("  Saved session username: ").strip()
    console.print(f"[dim]  Loading cookies for {username}...[/dim]")

    try:
        worker.init_client(username=username)
        proxy = ThreadSafeClientProxy(worker)
        is_valid, msg = validate_session(proxy)
        color = "green" if is_valid else "yellow"
        icon = "✅" if is_valid else "⚠️ "
        console.print(f"[bold {color}]  {icon} {msg}[/bold {color}]")
        if not is_valid:
            console.print("[dim]     Tip: Use Option 1 to log in and generate a fresh session.[/dim]")
        return proxy

    except FileNotFoundError:
        console.print(f"[bold red]  ❌ No saved session found for '{username}'.[/bold red]")
        console.print("[dim]     Use Option 1 to log in and save a session.[/dim]")
    except PermissionError:
        console.print("[bold red]  ❌ Permission denied reading session file.[/bold red]")
    except Exception as e:
        console.print(f"[bold red]  ❌ {e}[/bold red]")

    console.print("[dim]  Falling back to Guest mode...[/dim]")
    return _init_guest(worker)


def _flow_guest_mode(worker: PlaywrightWorker) -> ThreadSafeClientProxy:
    console.print("[bold yellow]  ⚠️  Guest mode active. Severe rate-limits will apply.[/bold yellow]")
    return _init_guest(worker)


# =============================================================================
# BOOT
# =============================================================================

def boot():
    print_splash()

    # Worker thread is created once and kept alive for the entire session
    worker = PlaywrightWorker()

    console.print("\n[bold cyan]Select an Authentication Strategy:[/bold cyan]")
    console.print("  1. [bold white]Username & Password[/bold white]    (Login and save session)")
    console.print("  2. [bold white]Load Local Session[/bold white]     (Use existing cookie file)")
    console.print("  3. [bold white]Guest Mode[/bold white]             (Anonymous, severe rate-limits)")
    console.print("  4. [bold white]Exit[/bold white]")

    choice = input("\n  Choose [1-4]: ").strip()

    if choice == "4":
        console.print("[dim]Exiting...[/dim]")
        worker.stop()
        sys.exit(0)

    console.print()

    if choice == "1":
        client = _flow_login_password(worker)
    elif choice == "2":
        client = _flow_load_session(worker)
    else:
        client = _flow_guest_mode(worker)

    console.print("\n[dim]Launching shell...[/dim]\n")
    shell = IGDetectiveShell(client)

    try:
        shell.cmdloop()
    except KeyboardInterrupt:
        console.print("\n[bold red]  Interrupt received. Shutting down.[/bold red]")
    finally:
        try:
            worker.call(lambda c: c.close())
        except Exception:
            pass
        worker.stop()


if __name__ == "__main__":
    try:
        boot()
    except KeyboardInterrupt:
        Console().print("\n[bold red]Operation cancelled by user.[/bold red]")
        sys.exit(0)