"""TCP -> docker-exec stdio SSH bridge for Herdr Mobile (phone -> 1c sandbox).

Listens on the PC's WireGuard address; each connection spawns the same
stdio.sh channel Herdr uses locally (sshd -i inside 1c-ai-sandbox).
No container network reachability or published ports required.
"""
import socketserver
import subprocess
import threading

LISTEN = ('10.77.90.2', 2222)
DOCKER = r'C:\Program Files\Docker\Docker\resources\bin\docker.exe'
CMD = [DOCKER, 'exec', '-i', '-u', 'root', '1c-ai-sandbox',
       '/home/vscode/.local/state/herdr-ssh/stdio.sh']


def pump(src, dst):
    try:
        while True:
            # read1: return whatever is available; read(4096) would block
            # until the buffer fills and stall the SSH banner.
            data = src.read1(4096)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


LOG = __import__('pathlib').Path(__file__).with_name('phone-ssh-bridge.log')


def log(msg):
    with LOG.open('a', encoding='utf-8') as f:
        f.write(f'{__import__("datetime").datetime.now().isoformat()} {msg}\n')


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        log(f'accept {self.client_address}')
        p = subprocess.Popen(CMD, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                             creationflags=0x08000000)
        t = threading.Thread(target=pump, args=(p.stdout, self.request), daemon=True)
        t.start()
        try:
            while True:
                data = self.request.recv(4096)
                if not data:
                    break
                p.stdin.write(data)
                p.stdin.flush()
        except OSError:
            pass
        finally:
            try:
                p.stdin.close()
            except OSError:
                pass
            p.wait(timeout=10)
            t.join(timeout=5)
            log(f'closed rc={p.returncode} err={p.stderr.read()[:200]!r}')
            self.request.close()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == '__main__':
    with Server(LISTEN, Handler) as srv:
        srv.serve_forever()
