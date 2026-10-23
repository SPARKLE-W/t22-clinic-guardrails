"""Test-only AMQP proxy: filter real publisher confirms, optionally staying alive."""
import socket
import struct
import threading


class ConfirmLossProxy:
    def __init__(self,config,disconnect_on_confirm=True):
        self.config=config
        self.disconnect_on_confirm=disconnect_on_confirm
        self.sockets=[]
        self.dropped=threading.Event()

    def __enter__(self):
        self.listener=socket.socket()
        self.listener.bind(('127.0.0.1',0))
        self.port=self.listener.getsockname()[1]
        self.listener.listen(1)
        self.listener.settimeout(10)
        self.thread=threading.Thread(target=self.relay,daemon=True)
        self.thread.start()
        return self

    @staticmethod
    def read_exact(sock,count):
        result=b''
        while len(result)<count:
            block=sock.recv(count-len(result))
            if not block:
                raise EOFError()
            result+=block
        return result

    def relay(self):
        try:
            client,_=self.listener.accept()
            upstream=socket.create_connection((self.config['host'],self.config['port']),5)
            self.sockets.extend([client,upstream])
            client.settimeout(10);upstream.settimeout(10)
            def forward_client():
                try:
                    while block:=client.recv(65536):
                        upstream.sendall(block)
                except OSError:
                    pass
            threading.Thread(target=forward_client,daemon=True).start()
            while True:
                header=self.read_exact(upstream,7)
                kind,channel,size=struct.unpack('!BHI',header)
                payload=self.read_exact(upstream,size)
                end=self.read_exact(upstream,1)
                if kind==1 and channel and payload[:4]==b'\x00\x3c\x00\x50':
                    self.dropped.set()
                    if self.disconnect_on_confirm:
                        break
                    continue
                client.sendall(header+payload+end)
        except (OSError,EOFError):
            pass
        finally:
            for sock in self.sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()

    def disconnect(self):
        for sock in self.sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def __exit__(self,*args):
        self.disconnect()
        self.listener.close()
        self.thread.join(timeout=12)
