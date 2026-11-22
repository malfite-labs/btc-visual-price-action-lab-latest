from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from backend.book_pa_sidecar import create_server


def test_sidecar_read_only_and_no_result(tmp_path):
    server = create_server('127.0.0.1', 0, tmp_path / 'research.sqlite')
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/api/v1/research/book-pa/latest'
    try:
        with urlopen(url) as response:
            assert response.status == 200
            assert b'NO_RESEARCH_RESULTS' in response.read()
        with pytest.raises(HTTPError) as error:
            urlopen(Request(url, data=b'{}', method='POST'))
        assert error.value.code == 405
        assert not (tmp_path / 'research.sqlite').exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
