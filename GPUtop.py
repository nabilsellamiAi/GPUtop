import time
import msvcrt
import os
from collections import deque

import pynvml
import psutil



from rich.console import Console
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.progress import Progress, BarColumn


# ============================================================
# CONFIG
# ============================================================

GPU_INDEX = 0

REFRESH_RATE = 0.50
MIN_REFRESH = 0.10
MAX_REFRESH = 3.00

HISTORY_LENGTH = 240
MAIN_GRAPH_HEIGHT = 7
MINI_GRAPH_HEIGHT = 3


# Recommended terminal size for GPUtop
try:
    os.system("mode con: cols=145 lines=46")
except Exception:
    pass

console = Console()

gpu_history = deque(maxlen=HISTORY_LENGTH)
vram_history = deque(maxlen=HISTORY_LENGTH)
temp_history = deque(maxlen=HISTORY_LENGTH)
power_history = deque(maxlen=HISTORY_LENGTH)

process_cpu = {}

running = True


# ============================================================
# NVIDIA INITIALIZATION
# ============================================================

pynvml.nvmlInit()

handle = pynvml.nvmlDeviceGetHandleByIndex(GPU_INDEX)

gpu_name = pynvml.nvmlDeviceGetName(handle)

if isinstance(gpu_name, bytes):
    gpu_name = gpu_name.decode("utf-8")


# ============================================================
# HELPERS
# ============================================================

def safe(func, default=None):
    try:
        return func()
    except Exception:
        return default


def gb(value):
    if not isinstance(value, (int, float)):
        return 0.0

    return value / (1024 ** 3)


def clamp(value, low, high):
    return max(low, min(high, value))


def average(values):
    if not values:
        return 0

    return sum(values) / len(values)


# ============================================================
# TELEMETRY
# ============================================================

def read_gpu():

    util = safe(
        lambda: pynvml.nvmlDeviceGetUtilizationRates(handle)
    )

    mem = pynvml.nvmlDeviceGetMemoryInfo(handle)

    temperature = safe(
        lambda: pynvml.nvmlDeviceGetTemperature(
            handle,
            pynvml.NVML_TEMPERATURE_GPU
        )
    )

    power = safe(
        lambda: pynvml.nvmlDeviceGetPowerUsage(handle) / 1000
    )

    power_limit = safe(
        lambda: pynvml.nvmlDeviceGetEnforcedPowerLimit(handle) / 1000
    )

    gpu_clock = safe(
        lambda: pynvml.nvmlDeviceGetClockInfo(
            handle,
            pynvml.NVML_CLOCK_GRAPHICS
        )
    )

    memory_clock = safe(
        lambda: pynvml.nvmlDeviceGetClockInfo(
            handle,
            pynvml.NVML_CLOCK_MEM
        )
    )

    pstate = safe(
        lambda: pynvml.nvmlDeviceGetPerformanceState(handle)
    )

    encoder = safe(
        lambda: pynvml.nvmlDeviceGetEncoderUtilization(handle)[0]
    )

    decoder = safe(
        lambda: pynvml.nvmlDeviceGetDecoderUtilization(handle)[0]
    )

    pcie_gen = safe(
        lambda: pynvml.nvmlDeviceGetCurrPcieLinkGeneration(handle)
    )

    pcie_width = safe(
        lambda: pynvml.nvmlDeviceGetCurrPcieLinkWidth(handle)
    )

    fan = safe(
        lambda: pynvml.nvmlDeviceGetFanSpeed(handle)
    )

    return {
        "gpu": util.gpu if util else 0,
        "memory_util": util.memory if util else 0,

        "memory_used": mem.used,
        "memory_free": mem.free,
        "memory_total": mem.total,

        "temperature": temperature,

        "power": power,
        "power_limit": power_limit,

        "gpu_clock": gpu_clock,
        "memory_clock": memory_clock,

        "pstate": pstate,

        "encoder": encoder,
        "decoder": decoder,

        "pcie_gen": pcie_gen,
        "pcie_width": pcie_width,

        "fan": fan,
    }


# ============================================================
# BRAILLE GRAPH
#
# Each terminal cell gives us:
#
#   2 horizontal pixels
#   4 vertical pixels
#
# Unlike V3, we interpolate BETWEEN samples horizontally.
# This removes the ugly vertical dotted walls.
# ============================================================

BRAILLE_BASE = 0x2800

BRAILLE_DOTS = [
    [0x01, 0x08],
    [0x02, 0x10],
    [0x04, 0x20],
    [0x40, 0x80],
]


def resample(values, target_count):

    values = list(values)

    if not values:
        return [0.0] * target_count

    if len(values) == 1:
        return [values[0]] * target_count

    # Don't stretch a few initial samples across
    # the entire screen. Keep them right-aligned.
    if len(values) < target_count:

        missing = target_count - len(values)

        return (
            [None] * missing +
            [float(v) for v in values]
        )

    result = []

    source_max = len(values) - 1
    target_max = target_count - 1

    for i in range(target_count):

        position = (
            i / target_max
        ) * source_max

        left = int(position)

        right = min(
            left + 1,
            source_max
        )

        fraction = position - left

        value = (
            values[left] * (1 - fraction)
            +
            values[right] * fraction
        )

        result.append(value)

    return result


def braille_graph(
    values,
    minimum,
    maximum,
    width,
    height
):

    width = max(4, width)
    height = max(2, height)

    pixel_width = width * 2
    pixel_height = height * 4

    samples = resample(
        values,
        pixel_width
    )

    value_range = maximum - minimum

    if value_range <= 0:
        value_range = 1

    pixels = [
        [False] * pixel_width
        for _ in range(pixel_height)
    ]

    previous = None

    for x, value in enumerate(samples):

        if value is None:
            previous = None
            continue

        normalized = (
            (value - minimum) /
            value_range
        )

        normalized = clamp(
            normalized,
            0.0,
            1.0
        )

        y = round(
            (1.0 - normalized) *
            (pixel_height - 1)
        )

        pixels[y][x] = True

        # Smooth diagonal interpolation.
        if previous is not None:

            px, py = previous

            dx = x - px
            dy = y - py

            steps = max(
                abs(dx),
                abs(dy)
            )

            if steps > 0:

                for step in range(1, steps):

                    t = step / steps

                    ix = round(
                        px + dx * t
                    )

                    iy = round(
                        py + dy * t
                    )

                    if (
                        0 <= ix < pixel_width
                        and
                        0 <= iy < pixel_height
                    ):
                        pixels[iy][ix] = True

        previous = (
            x,
            y
        )

    output = []

    for char_y in range(height):

        line = ""

        for char_x in range(width):

            mask = 0

            for dy in range(4):

                for dx in range(2):

                    py = (
                        char_y * 4 + dy
                    )

                    px = (
                        char_x * 2 + dx
                    )

                    if pixels[py][px]:

                        mask |= (
                            BRAILLE_DOTS[dy][dx]
                        )

            line += chr(
                BRAILLE_BASE + mask
            )

        output.append(line)

    return output


# ============================================================
# AUTO SCALE
# ============================================================

def auto_scale(
    values,
    hard_min=None,
    hard_max=None,
    minimum_span=1.0,
    padding_ratio=0.20
):

    values = [
        value
        for value in values
        if value is not None
    ]

    if not values:

        low = (
            hard_min
            if hard_min is not None
            else 0
        )

        high = (
            hard_max
            if hard_max is not None
            else low + minimum_span
        )

        return low, high

    low = min(values)
    high = max(values)

    span = high - low

    desired_span = max(
        span,
        minimum_span
    )

    padding = max(
        span * padding_ratio,
        desired_span * 0.10
    )

    center = (
        low + high
    ) / 2

    desired_span += (
        padding * 2
    )

    low = (
        center -
        desired_span / 2
    )

    high = (
        center +
        desired_span / 2
    )

    if hard_min is not None:

        if low < hard_min:

            high += (
                hard_min - low
            )

            low = hard_min

    if hard_max is not None:

        if high > hard_max:

            low -= (
                high - hard_max
            )

            high = hard_max

    if hard_min is not None:
        low = max(
            hard_min,
            low
        )

    if hard_max is not None:
        high = min(
            hard_max,
            high
        )

    if high <= low:
        high = low + minimum_span

    return low, high


# ============================================================
# GRAPH PANEL
# ============================================================

def graph_panel(
    title,
    values,
    minimum,
    maximum,
    current_text,
    color,
    width,
    height,
    decimals=0,
    suffix=""
):

    graph_width = max(
        10,
        width - 11
    )

    lines = braille_graph(
        values,
        minimum,
        maximum,
        graph_width,
        height
    )

    text = Text()

    middle = height // 2

    for index, line in enumerate(lines):

        if index == 0:
            value = maximum

        elif index == middle:
            value = (
                minimum + maximum
            ) / 2

        elif index == height - 1:
            value = minimum

        else:
            value = None

        if value is None:
            label = ""

        else:
            label = (
                f"{value:.{decimals}f}"
                f"{suffix}"
            )

        text.append(
            f"{label:>7} │",
            style="dim"
        )

        text.append(
            line,
            style=color
        )

        if index != height - 1:
            text.append("\n")

    return Panel(
        text,
        title=(
            f"[{color}]{title}[/{color}] "
            f"[bold]{current_text}[/bold]"
        ),
        border_style=color
    )


# ============================================================
# PROGRESS BAR
# ============================================================

def make_bar(
    value,
    color
):

    progress = Progress(
        BarColumn(
            bar_width=None,
            complete_style=color,
            finished_style=color
        ),
        expand=True
    )

    task = progress.add_task(
        "",
        total=100
    )

    progress.update(
        task,
        completed=clamp(
            value,
            0,
            100
        )
    )

    return progress


# ============================================================
# HEADER
# ============================================================

def make_header(data):

    used = gb(
        data["memory_used"]
    )

    total = gb(
        data["memory_total"]
    )

    vram_percent = (
        data["memory_used"] /
        data["memory_total"] *
        100
    )

    power_percent = 0

    if (
        data["power"] is not None
        and
        data["power_limit"]
    ):

        power_percent = (
            data["power"] /
            data["power_limit"] *
            100
        )

    table = Table.grid(
        expand=True,
        padding=(0, 1)
    )

    table.add_column(
        width=8
    )

    table.add_column(
        ratio=1
    )

    table.add_column(
        width=25,
        justify="right"
    )

    table.add_row(
        Text(
            "GPU",
            style="bold cyan"
        ),

        make_bar(
            data["gpu"],
            "cyan"
        ),

        Text(
            f"{data['gpu']}%",
            style="bold cyan"
        )
    )

    vram_color = (
        "red"
        if vram_percent >= 95
        else "magenta"
    )

    table.add_row(
        Text(
            "VRAM",
            style=f"bold {vram_color}"
        ),

        make_bar(
            vram_percent,
            vram_color
        ),

        Text(
            f"{used:.2f} / {total:.2f} GB",
            style=f"bold {vram_color}"
        )
    )

    table.add_row(
        Text(
            "POWER",
            style="bold yellow"
        ),

        make_bar(
            power_percent,
            "yellow"
        ),

        Text(
            (
                f"{data['power']:.1f} / "
                f"{data['power_limit']:.0f} W"
            )
            if (
                data["power"] is not None
                and
                data["power_limit"] is not None
            )
            else "N/A",
            style="yellow"
        )
    )

    return Panel(
        table,
        title=(
            "[bold cyan] GPUtop 1.0 [/bold cyan]"
            f"  [bold]{gpu_name}[/bold]"
        ),
        border_style="cyan"
    )


# ============================================================
# DETAIL PANELS
# ============================================================

def make_engine(data):

    table = Table.grid(
        expand=True
    )

    table.add_column()
    table.add_column(
        justify="right"
    )

    pstate = (
        f"P{data['pstate']}"
        if data["pstate"] is not None
        else "N/A"
    )

    table.add_row(
        "GPU",
        f"[bold cyan]{data['gpu']}%[/bold cyan]"
    )

    table.add_row(
        "Memory engine",
        (
            f"{data['memory_util']}%"
            if data["memory_util"] is not None
            else "N/A"
        )
    )

    table.add_row(
        "Encoder",
        (
            f"{data['encoder']}%"
            if data["encoder"] is not None
            else "N/A"
        )
    )

    table.add_row(
        "Decoder",
        (
            f"{data['decoder']}%"
            if data["decoder"] is not None
            else "N/A"
        )
    )

    table.add_row(
        "P-State",
        pstate
    )

    return Panel(
        table,
        title="ENGINE",
        border_style="green"
    )


def make_memory(data):

    used = gb(
        data["memory_used"]
    )

    free = gb(
        data["memory_free"]
    )

    total = gb(
        data["memory_total"]
    )

    percent = (
        data["memory_used"] /
        data["memory_total"] *
        100
    )

    color = (
        "red"
        if percent >= 95
        else "magenta"
    )

    table = Table.grid(
        expand=True
    )

    table.add_column()
    table.add_column(
        justify="right"
    )

    table.add_row(
        "Used",
        Text(
            f"{used:.2f} GB",
            style=f"bold {color}"
        )
    )

    table.add_row(
        "Free",
        f"{free:.2f} GB"
    )

    table.add_row(
        "Total",
        f"{total:.2f} GB"
    )

    table.add_row(
        "Usage",
        Text(
            f"{percent:.1f}%",
            style=f"bold {color}"
        )
    )

    return Panel(
        table,
        title="MEMORY",
        border_style=color
    )


def make_hardware(data):

    table = Table.grid(
        expand=True
    )

    table.add_column()
    table.add_column(
        justify="right"
    )

    temperature = data[
        "temperature"
    ]

    if temperature is None:

        temperature_text = Text(
            "N/A"
        )

    elif temperature >= 85:

        temperature_text = Text(
            f"{temperature} °C",
            style="bold red"
        )

    elif temperature >= 75:

        temperature_text = Text(
            f"{temperature} °C",
            style="bold yellow"
        )

    else:

        temperature_text = Text(
            f"{temperature} °C",
            style="green"
        )

    table.add_row(
        "Temperature",
        temperature_text
    )

    table.add_row(
        "Power",
        (
            f"{data['power']:.1f} W"
            if data["power"] is not None
            else "N/A"
        )
    )

    table.add_row(
        "Power limit",
        (
            f"{data['power_limit']:.0f} W"
            if data["power_limit"] is not None
            else "N/A"
        )
    )

    table.add_row(
        "GPU clock",
        (
            f"{data['gpu_clock']} MHz"
            if data["gpu_clock"] is not None
            else "N/A"
        )
    )

    table.add_row(
        "Memory clock",
        (
            f"{data['memory_clock']} MHz"
            if data["memory_clock"] is not None
            else "N/A"
        )
    )

    return Panel(
        table,
        title="HARDWARE",
        border_style="yellow"
    )


def make_system(data):

    table = Table.grid(
        expand=True
    )

    table.add_column()
    table.add_column(
        justify="right"
    )

    if (
        data["pcie_gen"] is not None
        and
        data["pcie_width"] is not None
    ):

        pcie = (
            f"Gen {data['pcie_gen']} "
            f"x{data['pcie_width']}"
        )

    else:
        pcie = "N/A"

    fan = (
        f"{data['fan']}%"
        if data["fan"] is not None
        else "EC controlled"
    )

    table.add_row(
        "PCIe",
        pcie
    )

    table.add_row(
        "Fan",
        fan
    )

    table.add_row(
        "Refresh",
        f"{REFRESH_RATE:.2f} s"
    )

    table.add_row(
        "Samples",
        str(
            len(gpu_history)
        )
    )

    return Panel(
        table,
        title="SYSTEM",
        border_style="blue"
    )


# ============================================================
# SESSION STATISTICS
# ============================================================

def stat_row(
    table,
    name,
    history,
    formatter,
    color
):

    if not history:

        table.add_row(
            name,
            "-",
            "-",
            "-"
        )

        return

    values = list(history)

    table.add_row(

        Text(
            name,
            style=color
        ),

        formatter(
            values[-1]
        ),

        formatter(
            average(values)
        ),

        formatter(
            max(values)
        )
    )


def make_stats():

    table = Table(
        expand=True,
        box=None,
        padding=(0, 1)
    )

    table.add_column(
        "",
        width=8
    )

    table.add_column(
        "CURRENT",
        justify="right"
    )

    table.add_column(
        "AVG",
        justify="right"
    )

    table.add_column(
        "PEAK",
        justify="right"
    )

    stat_row(
        table,
        "GPU",
        gpu_history,
        lambda v: f"{v:.0f}%",
        "cyan"
    )

    stat_row(
        table,
        "VRAM",
        vram_history,
        lambda v: f"{v:.2f}G",
        "magenta"
    )

    stat_row(
        table,
        "TEMP",
        temp_history,
        lambda v: f"{v:.0f}°C",
        "yellow"
    )

    stat_row(
        table,
        "POWER",
        power_history,
        lambda v: f"{v:.1f}W",
        "green"
    )

    return Panel(
        table,
        title="SESSION",
        border_style="cyan"
    )


# ============================================================
# PROCESS MONITOR
# ============================================================

def get_gpu_processes():

    global process_cpu

    result = {}

    functions = [

        getattr(
            pynvml,
            "nvmlDeviceGetGraphicsRunningProcesses",
            None
        ),

        getattr(
            pynvml,
            "nvmlDeviceGetComputeRunningProcesses",
            None
        )
    ]

    for function in functions:

        if function is None:
            continue

        try:
            processes = function(
                handle
            )

        except Exception:
            continue

        for proc in processes:

            pid = proc.pid

            memory = getattr(
                proc,
                "usedGpuMemory",
                None
            )

            if (
                memory is None
                or
                not isinstance(memory, int)
                or
                memory < 0
                or
                memory > 1024 ** 5
            ):
                memory = None

            if pid not in result:

                result[pid] = memory

            elif (
                memory is not None
                and
                (
                    result[pid] is None
                    or
                    memory > result[pid]
                )
            ):

                result[pid] = memory

    rows = []

    current_pids = set(
        result.keys()
    )

    # Remove dead processes from cache
    for pid in list(
        process_cpu.keys()
    ):

        if pid not in current_pids:

            del process_cpu[pid]

    for pid, memory in result.items():

        try:

            proc = process_cpu.get(
                pid
            )

            if proc is None:

                proc = psutil.Process(
                    pid
                )

                # Prime CPU measurement
                proc.cpu_percent(
                    interval=None
                )

                process_cpu[pid] = proc

                cpu = 0.0

            else:

                cpu = proc.cpu_percent(
                    interval=None
                )

            name = proc.name()

            ram = (
                proc.memory_info().rss
            )

        except Exception:

            name = "Unknown"
            cpu = 0
            ram = 0

        rows.append(
            (
                pid,
                name,
                memory,
                cpu,
                ram
            )
        )

    rows.sort(
        key=lambda row:
        row[4],
        reverse=True
    )

    return rows


def make_process_panel():

    rows = get_gpu_processes()

    table = Table(
        expand=True,
        box=None,
        padding=(0, 1)
    )

    table.add_column(
        "PID",
        width=8,
        style="dim"
    )

    table.add_column(
        "PROCESS",
        ratio=1
    )

    table.add_column(
        "CPU",
        width=8,
        justify="right"
    )

    table.add_column(
        "RAM",
        width=10,
        justify="right"
    )

    table.add_column(
        "VRAM*",
        width=10,
        justify="right"
    )

    if not rows:

        table.add_row(
            "-",
            "No active NVIDIA processes",
            "-",
            "-",
            "-"
        )

    for (
        pid,
        name,
        memory,
        cpu,
        ram
    ) in rows[:8]:

        memory_text = (
            f"{gb(memory):.2f}G"
            if memory is not None
            else "—"
        )

        table.add_row(
            str(pid),
            name,
            f"{cpu:.1f}%",
            f"{gb(ram):.2f}G",
            memory_text
        )

    return Panel(
        table,
        title=(
            "GPU PROCESSES "
            "[dim](* WDDM may hide per-process VRAM)[/dim]"
        ),
        border_style="cyan"
    )


# ============================================================
# MINI GRAPH
# ============================================================

def make_mini_graph(
    title,
    history,
    current,
    color,
    suffix,
    minimum_span
):

    if history:

        low, high = auto_scale(
            history,
            minimum_span=minimum_span
        )

    else:

        low = 0
        high = minimum_span

    width = max(
        12,
        console.width // 3 - 10
    )

    lines = braille_graph(
        history,
        low,
        high,
        width,
        MINI_GRAPH_HEIGHT
    )

    text = Text()

    for index, line in enumerate(
        lines
    ):

        if index == 0:

            label = (
                f"{high:.1f}"
            )

        elif index == (
            len(lines) - 1
        ):

            label = (
                f"{low:.1f}"
            )

        else:

            label = ""

        text.append(
            f"{label:>6} │",
            style="dim"
        )

        text.append(
            line,
            style=color
        )

        if index != (
            len(lines) - 1
        ):

            text.append("\n")

    return Panel(
        text,
        title=(
            f"[{color}]{title}[/{color}] "
            f"[bold]{current}{suffix}[/bold]"
        ),
        border_style=color
    )


# ============================================================
# KEYBOARD
# ============================================================

def reset_history():

    gpu_history.clear()
    vram_history.clear()
    temp_history.clear()
    power_history.clear()


def handle_keyboard():

    global running
    global REFRESH_RATE

    while msvcrt.kbhit():

        key = (
            msvcrt.getwch()
            .lower()
        )

        if key == "q":

            running = False

        elif key == "r":

            reset_history()

        elif key in (
            "+",
            "="
        ):

            REFRESH_RATE = max(
                MIN_REFRESH,
                REFRESH_RATE - 0.10
            )

        elif key == "-":

            REFRESH_RATE = min(
                MAX_REFRESH,
                REFRESH_RATE + 0.10
            )


# ============================================================
# BUILD DASHBOARD
# ============================================================

def build_dashboard():

    data = read_gpu()

    # --------------------------------------------------------
    # RECORD HISTORY
    # --------------------------------------------------------

    gpu_history.append(
        data["gpu"]
    )

    vram_history.append(
        gb(
            data["memory_used"]
        )
    )

    if data["temperature"] is not None:

        temp_history.append(
            data["temperature"]
        )

    if data["power"] is not None:

        power_history.append(
            data["power"]
        )

    # --------------------------------------------------------
    # LAYOUT
    # --------------------------------------------------------

    layout = Layout()

    layout.split_column(

        Layout(
            name="header",
            size=7
        ),

        Layout(
            name="graphs",
            size=11
        ),

        Layout(
            name="details",
            size=8
        ),

        Layout(
            name="lower",
            size=7
        ),

        Layout(
            name="processes"
        ),

        Layout(
            name="footer",
            size=1
        )
    )

    # --------------------------------------------------------
    # HEADER
    # --------------------------------------------------------

    layout[
        "header"
    ].update(
        make_header(data)
    )

    # --------------------------------------------------------
    # MAIN GRAPHS
    # --------------------------------------------------------

    layout[
        "graphs"
    ].split_row(

        Layout(
            name="gpu_graph"
        ),

        Layout(
            name="vram_graph"
        )
    )

    graph_panel_width = max(
        30,
        console.width // 2 - 3
    )

    layout[
        "gpu_graph"
    ].update(

        graph_panel(
            "GPU HISTORY",
            gpu_history,
            0,
            100,
            f"{data['gpu']}%",
            "green",
            graph_panel_width,
            MAIN_GRAPH_HEIGHT,
            decimals=0,
            suffix="%"
        )
    )

    total_vram = gb(
        data["memory_total"]
    )

    vram_low, vram_high = (
        auto_scale(
            vram_history,
            hard_min=0,
            hard_max=total_vram,
            minimum_span=1.0,
            padding_ratio=0.25
        )
    )

    layout[
        "vram_graph"
    ].update(

        graph_panel(
            "VRAM AUTO-ZOOM",
            vram_history,
            vram_low,
            vram_high,
            (
                f"{gb(data['memory_used']):.2f} GB"
            ),
            "magenta",
            graph_panel_width,
            MAIN_GRAPH_HEIGHT,
            decimals=1,
            suffix="G"
        )
    )

    # --------------------------------------------------------
    # DETAILS
    # --------------------------------------------------------

    layout[
        "details"
    ].split_row(

        Layout(
            make_engine(data)
        ),

        Layout(
            make_memory(data)
        ),

        Layout(
            make_hardware(data)
        ),

        Layout(
            make_system(data)
        )
    )

    # --------------------------------------------------------
    # LOWER SECTION
    # --------------------------------------------------------

    layout[
        "lower"
    ].split_row(

        Layout(
            make_mini_graph(
                "TEMP",
                temp_history,
                (
                    data["temperature"]
                    if data["temperature"] is not None
                    else "N/A"
                ),
                "yellow",
                "°C",
                5.0
            )
        ),

        Layout(
            make_mini_graph(
                "POWER",
                power_history,
                (
                    f"{data['power']:.1f}"
                    if data["power"] is not None
                    else "N/A"
                ),
                "cyan",
                "W",
                10.0
            )
        ),

        Layout(
            make_stats()
        )
    )

    # --------------------------------------------------------
    # PROCESSES
    # --------------------------------------------------------

    layout[
        "processes"
    ].update(
        make_process_panel()
    )

    # --------------------------------------------------------
    # FOOTER
    # --------------------------------------------------------

    footer = Text(
        justify="center"
    )

    footer.append(
        "Q ",
        style="bold cyan"
    )

    footer.append(
        "Quit   "
    )

    footer.append(
        "R ",
        style="bold cyan"
    )

    footer.append(
        "Reset   "
    )

    footer.append(
        "+ ",
        style="bold green"
    )

    footer.append(
        "Faster   "
    )

    footer.append(
        "- ",
        style="bold yellow"
    )

    footer.append(
        "Slower   "
    )

    footer.append(
        f"│ {REFRESH_RATE:.2f}s",
        style="dim"
    )

    layout[
        "footer"
    ].update(
        footer
    )

    return layout


# ============================================================
# MAIN
# ============================================================

try:

    console.clear()

    with Live(
        build_dashboard(),
        console=console,
        screen=True,
        auto_refresh=False
    ) as live:

        while running:

            frame_start = (
                time.perf_counter()
            )

            handle_keyboard()

            if not running:
                break

            live.update(
                build_dashboard(),
                refresh=True
            )

            elapsed = (
                time.perf_counter()
                -
                frame_start
            )

            remaining = max(
                0.01,
                REFRESH_RATE - elapsed
            )

            end_time = (
                time.perf_counter()
                +
                remaining
            )

            while (
                running
                and
                time.perf_counter()
                <
                end_time
            ):

                handle_keyboard()

                sleep_time = min(
                    0.03,
                    max(
                        0,
                        end_time
                        -
                        time.perf_counter()
                    )
                )

                if sleep_time > 0:
                    time.sleep(
                        sleep_time
                    )


except KeyboardInterrupt:
    pass


finally:

    pynvml.nvmlShutdown()

    console.clear()

    console.print(
        "[bold cyan]GPUtop 1.0[/bold cyan] stopped."
    )