# mypy: ignore-errors
from ctypes.wintypes import DWORD, BOOL, HANDLE
import ctypes
import sys
import psutil

CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32

def send_signal(pid, signal_type):
    _OpenProcess = kernel32.OpenProcess
    _OpenProcess.argtypes = [DWORD, BOOL, DWORD]
    _OpenProcess.restype  = HANDLE
    # THIS fails with:
    # Failed to attach to process PID console
    process_handle = _OpenProcess(0x00010000, False, pid)
    # process_handle = _OpenProcess(1, False, pid)
    if not process_handle:
        print(f"Failed to open process {pid}")
        return

    if signal_type in [CTRL_C_EVENT, CTRL_BREAK_EVENT]:
        if not kernel32.AttachConsole(pid):
            print(f"Failed to attach to process {pid} console")
            return

        # Send the control signal
        if not kernel32.GenerateConsoleCtrlEvent(signal_type, 0):
            print(f"Failed to send signal {signal_type} to process {pid}")
            return

        # Allow some time for the signal to be processed
        kernel32.Sleep(1000)

        # Detach from the console
        kernel32.FreeConsole()
    # elif signal_type == CTRL_CLOSE_EVENT:
    #     # Post a WM_CLOSE message to the window of the target process
    #     hwnd = user32.FindWindowA(None, None)
    #     while hwnd:
    #         pid_hwnd = ctypes.c_ulong()
    #         user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_hwnd))
    #         if pid_hwnd.value == pid:
    #             user32.PostMessageA(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    #             break
    #         hwnd = user32.GetWindow(hwnd, 2)  # GW_HWNDNEXT
    # elif signal_type in [CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT]:
    #     print(f"Simulating logoff/shutdown event is not supported directly via ctypes.")
    #     return

    print(f"Signal {signal_type} sent to process {pid}")

def find_process_id_by_name(process_name):
    for proc in psutil.process_iter(['pid', 'name']):
        print(f"XXX prc: {proc}")
        sys.stdout.flush()
        if proc.info['name'].lower() == process_name.lower():
            return proc.info['pid']
    return None

def find_oldest_process_by_name(process_name):
    oldest_process = None
    oldest_creation_time = None

    for proc in psutil.process_iter(['pid', 'name', 'create_time']):
        if proc.info['name'].lower() == process_name.lower():
            print(f"proc: {proc}")
            if oldest_creation_time is None or proc.info['create_time'] < oldest_creation_time:
                oldest_creation_time = proc.info['create_time']
                oldest_process = proc

    return oldest_process.info['pid']

def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <process_name> <signal_type>")
        print("Signal types: ctrl-c, ctrl-break, ctrl-close, logoff, ctrl-shutdown")
        return

    process_name = sys.argv[1]
    signal_type_str = sys.argv[2].lower()

    signal_map = {
        'ctrl-c': CTRL_C_EVENT,
        'ctrl-break': CTRL_BREAK_EVENT,
        'ctrl-close': CTRL_CLOSE_EVENT,
        'logoff': CTRL_LOGOFF_EVENT,
        'ctrl-shutdown': CTRL_SHUTDOWN_EVENT
    }

    if signal_type_str not in signal_map:
        print(f"Unknown signal type: {signal_type_str}")
        return

    signal_type = signal_map[signal_type_str]

    pid = find_oldest_process_by_name(process_name)
    if pid is None:
        print(f"Process {process_name} not found")
        return

    send_signal(pid, signal_type)

if __name__ == '__main__':
    main()

