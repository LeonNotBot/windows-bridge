"""Optional Windows Service host; console mode remains the default."""

import json
import os
from pathlib import Path

import win32service
import win32serviceutil
import uvicorn

from bridge import create_app
from config import load_targets, mock_targets


class BridgeService(win32serviceutil.ServiceFramework):
    _svc_name_ = "EdisonWindowsBridge"
    _svc_display_name_ = "Edison Windows Bridge"
    _svc_description_ = "Runs preconfigured Windows benchmark connectors."

    def __init__(self, args):
        super().__init__(args)
        self.server = None
        self.stopping = False

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.stopping = True
        if self.server:
            self.server.should_exit = True

    def SvcDoRun(self):
        path = Path(
            os.environ.get(
                "EDISON_BRIDGE_SERVICE_CONFIG", str(Path(__file__).parent / "bridge-service.json")
            )
        ).resolve()
        config = json.loads(path.read_text(encoding="utf-8"))
        resolve = lambda value: (path.parent / value).resolve()
        token = resolve(config["token_file"]).read_text(encoding="utf-8").strip()
        targets = (
            mock_targets()
            if config.get("mock")
            else load_targets(resolve(config["targets_config"]))
        )
        app = create_app(token, data_dir=resolve(config["data_dir"]), targets=targets)
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=config.get("port", 19827), access_log=False)
        )
        if not self.stopping:
            self.server.run()


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(BridgeService)
