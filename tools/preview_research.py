"""V8 UI fixture: actual HTTP builders, temp-only synthetic journals; financial POST forbidden."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"tests"))
from trading_lab.arena import Arena, write
from trading_lab.strategy_contract import payload_of
from trading_lab.strategy_training import SYNTHETIC
from trading_lab.web_panel import Handler, Panel, Server
from test_management_integration import recipe
from test_replay_evidence_integration import paper_fixture


class PreviewHandler(Handler):
    def do_POST(self):
        if self.path != "/api/workshop/tools": return self.send(405, {"error": "Only fake diagnostics; financial actions forbidden"})
        super().do_POST()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8878)
    parser.add_argument("--seconds", type=int, default=600)
    args=parser.parse_args()
    repository=Path(__file__).resolve().parents[1]
    def forbidden(*args, **kwargs): raise AssertionError("Actual transport/financial action forbidden")
    with tempfile.TemporaryDirectory(prefix="arena-v8-qa-") as directory:
        root=Path(directory)
        shutil.copytree(repository/"web",root/"web")
        arena=Arena(root,background=False,feed_factory=forbidden)
        journal=paper_fixture();journal["fixture_only"]=True
        arena.data["seasons"].append(journal["id"])
        write(arena.path/(journal["id"]+".json"),journal)
        with Server(Panel(root,launcher=forbidden,balance_reader=forbidden),args.port,arena=arena) as server:
            server.RequestHandlerClass=PreviewHandler
            version=server.workshop.add_version(server.workshop.data["generation"],payload_of(recipe()))["id"]
            training=server.workshop.train(server.workshop.data["generation"],version,SYNTHETIC["id"],100)
            timer=threading.Timer(args.seconds,server.shutdown);timer.daemon=True;timer.start()
            print(json.dumps({"origin":server.origin,"id":journal["id"],"version":version,"training":training,"fixture_only":True}),flush=True)
            try: server.serve_forever(poll_interval=.2)
            except KeyboardInterrupt: pass
            finally: timer.cancel()


if __name__=="__main__": main()
