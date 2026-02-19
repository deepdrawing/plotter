import sys
import math
import json
import time
import serial
import requests
import random


# Settings
PORT = "/dev/cu.usbmodem201912341"
BAUD = 115200
DATASET_URL = "https://storage.googleapis.com/quickdraw_dataset/full/raw/"
SPEED_MULTIPLIER = 20  # 1.0 = real time, higher value = slower drawing

SCALE = 0.5  # Pixels to mm conversion
BASE_MARGIN = 10  # 10 mm

X_MAX = 600  # 594     # 604
Y_MAX = 600  # 840.74  # 850 
BOUNDS = 250  # 590
PEN_UP = 0
PEN_DOWN = -8

REAL_SERIAL_PORT = True


class Scaler:
    """Scales coordinate values given mins, maxes, and margins"""
    def __init__(self, min_x, max_x, min_y, max_y, bounds=590):
        # 1. original dimensions
        x_range = max_x - min_x
        y_range = max_y - min_y

        # 2. Determine the aspect-ratio-preserving scale factor
        # Scale based on the larger dimension to ensure it fits the bounds
        master_scale = bounds / max(x_range, y_range)

        # 3. Randomize the "inner" scale and position
        # 'smaller_scale' makes it potentially smaller than the full 600px
        self.smaller_scale = random.uniform(0.25, 0.75)

        # Calculate resulting size in pixels
        final_w = x_range * master_scale * self.smaller_scale
        final_h = y_range * master_scale * self.smaller_scale

        # 4. Randomize position (padding is the leftover space in the 600x600 box)
        max_offset_x = bounds - final_w
        max_offset_y = bounds - final_h

        self.offset_x = random.uniform(0, max_offset_x)
        self.offset_y = random.uniform(0, max_offset_y)

        # Store constants for the transformation formula
        self.min_x = min_x
        self.min_y = min_y
        self.multiplier = master_scale * self.smaller_scale

    def scale(self, x, y):
        """Applies the pre-calculated random normalization to a point."""
        # Shift to 0, scale up, then apply the random offset
        new_x = (x - self.min_x) * self.multiplier + self.offset_x + 5
        new_y = - ((y - self.min_y) * self.multiplier + self.offset_y + 5)
        return new_x, new_y


class MockSerialPort:
    """
    Substitute serial port for testing when the plotter is unavailable
    """
    def write(self, data):
        # Print the data being sent to the "plotter"
        print(f"[SERIAL SEND] {data.decode().strip()}")

    @property
    def in_waiting(self):
        return 0  # so the loop doesn't hang waiting for a response

    def close(self):
        print("[SERIAL] Connection closed.")


def send(s, cmd):
    """
    Execute gcode command to iDraw plotter
    """
    print(">>", cmd)
    s.write((cmd + "\n").encode())

    if s is not MockSerialPort:
        # Wait for the plotter to respond with 'ok'
        while True:
            line = s.readline().decode().strip()
            if line:
                print(f"[{line}]") # This will show 'ok' or 'error'
            if "ok" in line.lower():
                break
            if "error" in line.lower() or "alarm" in line.lower():
                print(f"!!! MACHINE ERROR: {line}")
                break
    else:
        time.sleep(0.05)
        while s.in_waiting:
            print(s.readline().decode().strip())


def plot_drawing(s, drawing_data):
    """
    Docstring for plot_drawing
    """
    # 'drawing' is a list of strokes: [[x1, x2, ...], [y1, y2, ...]]
    strokes = drawing_data['drawing']

    min_x = math.inf
    max_x = -math.inf
    min_y = math.inf
    max_y = -math.inf
    for stroke in strokes:
        min_x = min(min_x, min(stroke[0]))
        max_x = max(max_x, max(stroke[0]))
        min_y = min(min_y, min(stroke[1]))
        max_y = max(max_y, max(stroke[1]))
    scaler = Scaler(min_x, max_x, min_y, max_y, bounds=BOUNDS)

    for stroke in strokes:
        x_coords = stroke[0] # pixel x
        y_coords = stroke[1] # pixel y
        timestamps = stroke[2] # in cumulative milliseconds since the first stroke

        # Step 1. move plotter to start of stroke
        send(s, f"G1 Z{PEN_UP} F7000")  # pen up
        start_x, start_y = scaler.scale(x_coords[0], y_coords[0])
        send(s, f"G0 X{start_x:.3f} Y{start_y:.3f} F11500")  # move to start of stroke

        # Step 2. perform the stroke
        send(s, f"G1 Z{PEN_DOWN} F7000")  # pen down

        for i in range(1, len(x_coords)):
            # Calculate timestamp delay between these two points
            delta_ms = timestamps[i] - timestamps[i-1]
            delay_seconds = delta_ms / 1000.0

            if delay_seconds <= 0:
                continue

            x, y = scaler.scale(x_coords[i], y_coords[i])
            x_prior, y_prior = scaler.scale(x_coords[i-1], y_coords[i-1])
            velocity = math.sqrt(((x - x_prior) ** 2) + ((y - y_prior) ** 2)) / delay_seconds
            #delta_x = (x_coords[i] - x_coords[i-1]) * SCALE
            #delta_y = (y_coords[i] - y_coords[i-1]) * SCALE
            #velocity = math.sqrt((delta_x ** 2) + (delta_y ** 2)) / delay_seconds

            if velocity <= 0:
                continue

            # Send the move command
            # x = x_coords[i] * SCALE
            # y = y_coords[i] * -SCALE
            clamped_velocity = min(velocity * SPEED_MULTIPLIER, 11500)
            send(s, f"G1 X{x:.3f} Y{y:.3f} F{clamped_velocity}")

    send(s, f"G1 Z{PEN_UP} F7000") # pen up


def get_ndjson(filename):
    """
    Return the url of this name from the raw Quick, Draw! dataset
    """
    return DATASET_URL + filename + ".ndjson"


def stream_and_plot(s, url, limit=1):
    """
    Stream ndjson from the web and plots the first 'limit' drawings.
    """
    try:
        # waits 5s to connect and 5s for the first byte
        # stream=True keeps connection open without downloading the whole file
        response = requests.get(url, stream=True, timeout=5)
        response.raise_for_status() # Check if URL is valid (404, etc)
    except requests.exceptions.Timeout:
        sys.exit("Error: The connection timed out. Please check your internet.")
    except requests.exceptions.RequestException as e:
        sys.exit(f"Error: A network error occurred: {e}")

    count = 0
    # iter_lines() handles the Newline Delimited (ndjson) format
    for line in response.iter_lines():
        if line and count < limit:
            drawing_data = json.loads(line)
            print(f"\n--- Starting Drawing {count+1}: {drawing_data['word']} ---")

            plot_drawing(s, drawing_data)

            count += 1
        elif count >= limit:
            break


def reset_plotter(s):
    """Reset and unlock plotter"""
    print("Sending reset and unlock...")
    # 1. Send Soft Reset (hex 0x18 is CTRL+X)
    s.write(b'\x18')
    time.sleep(2)

    # 2. Send Unlock command to clear the 'Reset to continue' alarm
    s.write(b"$X\n")
    time.sleep(1)

    # 3. Clear the buffer of all the startup "Welcome" text
    s.reset_input_buffer()
    print("Plotter unlocked.")


def main():
    """
    Docstring for main
    """
    try:
        s = serial.Serial(PORT, BAUD, timeout=1)
        time.sleep(2)
        reset_plotter(s)
    except serial.SerialException as e:
        print(f"Failed to open serial port:\n{e}")
        REAL_SERIAL_PORT = False
        s = MockSerialPort()

    # Setup
    send(s, "G21")  # Millimeters mode
    send(s, "G90")  # Absolute positioning
    send(s, "$32=0")  # Disable laser mode
    send(s, "$20=0")  # Disable Soft Limits
    send(s, f"$130={X_MAX}")  # Set X Max
    send(s, f"$131={Y_MAX}")  # Set Y Max

    # IMPORTANT: MANUALLY move the plotter to the top-LEFT corner, pen UP,
    # The servo's KNOB is at its bottommost = 0
    send(s, "G10 L20 P1 X0 Y0 Z0")

    # Plot from ndjson
    # stream_and_plot(s, get_ndjson("The Eiffel Tower"), 1)
    stream_and_plot(s, get_ndjson("dragon"), 1)

    #send(s, "G1 X0 Y-840 F10000")
    #for i in range(50):
    #    send(s, f"G1 X0 Y{-840-i} F2000")
    #    print(i)
    #    time.sleep(2)
    
    # time.sleep(2)
    # send(s, "$$")
    # send(s, f"G1 Z{PEN_DOWN} F2000")
    # time.sleep(2)
    # time.sleep(1)
    # send(s, "G1 X0 Y0 F2000")
    # time.sleep(1)
    # send(s, "M3 S30")
    # send(s, "G1 X600 Y0 F2000")
    # time.sleep(1)
    # send(s, "G1 X0 Y0 F2000")
    # time.sleep(1)
    # send(s, "G1 X0 Y-600 F2000")

    send(s, f"G1 Z{PEN_UP} F7000")
    send(s, "G0 X0 Y0")

    s.close()


if __name__ == "__main__":
    main()
