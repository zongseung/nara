import argparse
import os
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description='nara web / Prefect comparison worker')
    parser.add_argument('command', choices=('serve', 'register', 'worker'))
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    os.environ.setdefault('PREFECT_API_URL', 'http://127.0.0.1:4200/api')
    os.environ.setdefault('PREFECT_SERVER_ALLOW_EPHEMERAL_MODE', 'false')
    if os.environ.get('PREFECT_AUTH_STRING'):
        os.environ.setdefault('PREFECT_API_AUTH_STRING', os.environ['PREFECT_AUTH_STRING'])
    match args.command:
        case 'serve':
            import uvicorn
            uvicorn.run('src.web.api:app', host='127.0.0.1', port=args.port)
        case 'register':
            from src.web.flows import register
            print(register())
        case 'worker':
            subprocess.run([sys.executable, '-m', 'prefect', 'worker', 'start', '--pool',
                            os.environ.get('NARA_WEB_POOL', 'nara-web'), '--type', 'process', '--limit', '1'], check=True)


if __name__ == '__main__':
    main()
