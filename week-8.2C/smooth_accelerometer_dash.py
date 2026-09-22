import os
import csv
import time
import threading
from collections import deque
from datetime import datetime

from arduino_iot_cloud import ArduinoCloudClient
from dash import Dash, dcc, html, Input, Output
from dash.exceptions import PreventUpdate
import plotly.graph_objects as go


# ============================================================
# CONFIGURATION
# ============================================================

# Keep credentials outside the Python file so they are not
# accidentally uploaded to GitHub.
DEVICE_ID = "DEVICE_ID"
SECRET_KEY = "SECRET_KEY"

# Maximum number of points displayed on the live graph.
MAX_GRAPH_POINTS = 150

# Dash checks the buffer every 100 ms.
# The graph only receives genuinely new samples.
GRAPH_UPDATE_MS = 100

# Save received accelerometer samples for Task 5C evidence.
CSV_FILE = "accelerometer_live_data.csv"


# ============================================================
# SMOOTH SENSOR BUFFER
# ============================================================

class SmoothSensorBuffer:
    """
    Stores incoming X, Y and Z values from Arduino Cloud.

    A complete sample is created only after fresh values have
    arrived for all three axes. Complete samples are placed
    into a queue for the Dash graph.
    """

    def __init__(self, csv_file):
        self.latest = {
            "x": None,
            "y": None,
            "z": None
        }

        # Records which axes have changed since the last full sample.
        self.updated_axes = set()

        # Samples waiting to be sent to Plotly Dash.
        self.pending = deque()

        # A lock prevents the Arduino thread and Dash thread
        # from changing the same data at the same time.
        self.lock = threading.Lock()

        self.csv_file = csv_file
        self._prepare_csv()

    def _prepare_csv(self):
        """Create the CSV file and header if necessary."""

        if not os.path.exists(self.csv_file):
            with open(self.csv_file, "w", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(
                    ["timestamp", "accelerometer_x",
                     "accelerometer_y", "accelerometer_z"]
                )

    def update_axis(self, axis, value):
        """
        Receive one new axis value.

        When X, Y and Z have all received fresh values,
        create one complete accelerometer sample.
        """

        try:
            value = float(value)
        except (TypeError, ValueError):
            return

        with self.lock:
            self.latest[axis] = value
            self.updated_axes.add(axis)

            # Wait until fresh X, Y and Z values are available.
            if self.updated_axes == {"x", "y", "z"}:

                timestamp = datetime.now().isoformat(
                    timespec="milliseconds"
                )

                sample = {
                    "time": timestamp,
                    "x": self.latest["x"],
                    "y": self.latest["y"],
                    "z": self.latest["z"]
                }

                self.pending.append(sample)

                # The next complete sample must contain
                # another fresh X, Y and Z value.
                self.updated_axes.clear()

                self._save_sample(sample)

    def _save_sample(self, sample):
        """Append each complete sample to the CSV data file."""

        with open(self.csv_file, "a", newline="") as file:
            writer = csv.writer(file)

            writer.writerow([
                sample["time"],
                sample["x"],
                sample["y"],
                sample["z"]
            ])

    def get_pending_samples(self):
        """
        Return all samples that arrived since the last
        Dash update and remove them from the queue.
        """

        with self.lock:

            if not self.pending:
                return []

            samples = list(self.pending)
            self.pending.clear()

            return samples


# Create one shared buffer.
sensor_buffer = SmoothSensorBuffer(CSV_FILE)


# ============================================================
# ARDUINO CLOUD CALLBACK FUNCTIONS
# ============================================================

def on_accelerometer_x_changed(client, value):
    sensor_buffer.update_axis("x", value)


def on_accelerometer_y_changed(client, value):
    sensor_buffer.update_axis("y", value)


def on_accelerometer_z_changed(client, value):
    sensor_buffer.update_axis("z", value)


# ============================================================
# ARDUINO CLOUD CONNECTION
# ============================================================

def run_arduino_cloud():
    """
    Connect to Arduino IoT Cloud and continuously listen
    for the three synchronised accelerometer variables.
    """

    if not DEVICE_ID or not SECRET_KEY:
        print("ERROR: Arduino Cloud credentials are missing.")
        print("Set ARDUINO_DEVICE_ID and ARDUINO_SECRET_KEY first.")
        return

    client = ArduinoCloudClient(
        device_id=DEVICE_ID,
        username=DEVICE_ID,
        password=SECRET_KEY,

        # Sync mode works well here because the Arduino client
        # runs inside its own background thread.
        sync_mode=True
    )

    client.register(
        "accelerometer_x",
        value=None,
        on_write=on_accelerometer_x_changed
    )

    client.register(
        "accelerometer_y",
        value=None,
        on_write=on_accelerometer_y_changed
    )

    client.register(
        "accelerometer_z",
        value=None,
        on_write=on_accelerometer_z_changed
    )

    print("Connecting to Arduino IoT Cloud...")

    # In synchronous mode start() connects, then update()
    # processes incoming cloud events.
    client.start()

    print("Arduino Cloud connected.")
    print("Move your phone to generate accelerometer data.")

    while True:
        client.update()
        time.sleep(0.05)


# ============================================================
# PLOTLY DASH SETUP
# ============================================================

app = Dash(__name__)


# Create the initial graph once.
# After this, we EXTEND the existing traces rather than
# rebuilding the complete figure every time.
figure = go.Figure()

figure.add_trace(
    go.Scatter(
        x=[],
        y=[],
        mode="lines",
        name="Accelerometer X"
    )
)

figure.add_trace(
    go.Scatter(
        x=[],
        y=[],
        mode="lines",
        name="Accelerometer Y"
    )
)

figure.add_trace(
    go.Scatter(
        x=[],
        y=[],
        mode="lines",
        name="Accelerometer Z"
    )
)

figure.update_layout(
    title="Live Smartphone Accelerometer",
    xaxis_title="Time",
    yaxis_title="Acceleration (m/s²)",
    uirevision="keep",
    margin=dict(l=60, r=30, t=70, b=60)
)


app.layout = html.Div(
    [
        html.H1("SIT225 Live Accelerometer Dashboard"),

        html.P(
            "Live X, Y and Z smartphone acceleration "
            "received through Arduino IoT Cloud."
        ),

        dcc.Graph(
            id="accelerometer-graph",
            figure=figure
        ),

        # Runs the Dash update callback every 100 ms.
        dcc.Interval(
            id="graph-update-timer",
            interval=GRAPH_UPDATE_MS,
            n_intervals=0
        )
    ]
)


# ============================================================
# REUSABLE SMOOTH UPDATE WRAPPER
# ============================================================

def create_smooth_update(buffer, max_points=150):
    """
    Convert newly buffered sensor samples into Plotly extendData.

    Only NEW points are sent to the browser. Plotly keeps the
    existing graph and appends these values to it.

    This avoids rebuilding the complete figure whenever new
    sensor data arrives.

    Parameters
    ----------
    buffer:
        SmoothSensorBuffer containing continuous sensor samples.

    max_points:
        Maximum number of points retained for each graph trace.

    Returns
    -------
    Plotly Dash extendData tuple:
        (new_data, trace_indices, maximum_points)
    """

    samples = buffer.get_pending_samples()

    if not samples:
        raise PreventUpdate

    times = [sample["time"] for sample in samples]

    x_values = [sample["x"] for sample in samples]
    y_values = [sample["y"] for sample in samples]
    z_values = [sample["z"] for sample in samples]

    # Three lists correspond to the three Plotly traces.
    new_data = {
        "x": [
            times,
            times,
            times
        ],
        "y": [
            x_values,
            y_values,
            z_values
        ]
    }

    trace_indices = [0, 1, 2]

    return new_data, trace_indices, max_points


# ============================================================
# DASH CALLBACK
# ============================================================

@app.callback(
    Output("accelerometer-graph", "extendData"),
    Input("graph-update-timer", "n_intervals")
)
def update_live_graph(n_intervals):
    """
    Called every 100 ms.

    Instead of recreating the graph, the callback asks the
    wrapper for only the samples that arrived since the
    previous update.
    """

    return create_smooth_update(
        sensor_buffer,
        MAX_GRAPH_POINTS
    )


# ============================================================
# MAIN PROGRAM
# ============================================================

if __name__ == "__main__":

    # Arduino Cloud runs in the background while Dash remains
    # in the main thread.
    cloud_thread = threading.Thread(
        target=run_arduino_cloud,
        daemon=True
    )

    cloud_thread.start()

    print("Starting Plotly Dash...")
    print("Open the displayed local address in your browser.")

    app.run(
        debug=False,
        use_reloader=False
    )