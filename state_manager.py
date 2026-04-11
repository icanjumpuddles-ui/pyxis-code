import os
import json
import threading
import time
import copy

class PyxisState:
    def __init__(self, sim_file, dt_file):
        self.sim_file = sim_file
        self.dt_file = dt_file
        self.lock = threading.Lock()

        self.sim_state = {}
        self.dt_state = {}

        self._load_initial_state()

        # Start background flusher
        self.flusher_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self.flusher_thread.start()

    def _load_initial_state(self):
        with self.lock:
            if os.path.exists(self.sim_file):
                try:
                    with open(self.sim_file, "r") as f:
                        self.sim_state = json.load(f)
                except Exception:
                    pass

            if os.path.exists(self.dt_file):
                try:
                    with open(self.dt_file, "r") as f:
                        self.dt_state = json.load(f)
                except Exception:
                    pass

    def get_state(self, state_type="SIM"):
        with self.lock:
            if state_type == "SIM":
                return copy.deepcopy(self.sim_state)
            elif state_type == "DT":
                return copy.deepcopy(self.dt_state)
            else:
                return {}

    def update_state(self, state_type, payload, replace=False):
        with self.lock:
            if state_type == "SIM":
                if replace:
                    self.sim_state = payload
                else:
                    self.sim_state.update(payload)
            elif state_type == "DT":
                if replace:
                    self.dt_state = payload
                else:
                    self.dt_state.update(payload)

    def _flush_loop(self):
        while True:
            time.sleep(5)
            self.flush()

    def flush(self):
        with self.lock:
            sim_copy = copy.deepcopy(self.sim_state)
            dt_copy = copy.deepcopy(self.dt_state)

        try:
            with open(self.sim_file, "w") as f:
                json.dump(sim_copy, f)
        except Exception:
            pass

        try:
            with open(self.dt_file, "w") as f:
                json.dump(dt_copy, f)
        except Exception:
            pass
