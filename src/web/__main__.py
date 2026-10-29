import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description='nara web comparison server')
    parser.add_argument('command', choices=('serve',))
    parser.add_argument('--port', type=int, default=8000)
    uvicorn.run('src.web.api:app', host='127.0.0.1', port=parser.parse_args().port)


if __name__ == '__main__':
    main()
