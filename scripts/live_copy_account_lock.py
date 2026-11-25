"""Windows account ownership for the explicitly armed Demo acceptance path."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from hashlib import sha256


@contextmanager
def account_owner(environment, exchange_account, symbol='BTCUSDT'):
    if environment not in ('PRODUCTION','DEMO','SIMULATED') or not isinstance(exchange_account,str) or not exchange_account.strip():
        raise ValueError('UNVERIFIED_ACCOUNT_IDENTITY')
    identity = sha256(f'BINANCE:USDM:{environment}:{exchange_account}:{symbol}'.encode()).hexdigest()
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateMutexW.argtypes = [ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR]
    api.CreateMutexW.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE,wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.ReleaseMutex.argtypes = [wintypes.HANDLE]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    mutex = api.CreateMutexW(None,False,'Global\\BTCV3-Copy-'+identity)
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        result = api.WaitForSingleObject(mutex,0)
        if result == 258:
            raise RuntimeError('ACCOUNT_ALREADY_OWNED')
        if result not in (0,128):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            api.ReleaseMutex(mutex)
    finally:
        api.CloseHandle(mutex)
