#!/usr/bin/env python3 test

import copy
import random
import json
import logging
import re
import socket
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request

BASE_DIR = Path("/root/VibeChat")
TEMPLATES_DIR = BASE_DIR / "templates"
BRIDGE_LOG = BASE_DIR / "logs/sonic-ai-bridge.log"
HEADLESS_LOG = BASE_DIR / "logs/sonic-pi-headless.log"
STATE_FILE = BASE_DIR / "ai_state.json"

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
WEB_PORT = 8081
#MODEL = "qwen2.5:1.5b"
MODEL = "qwen2.5:7b"

# --- Evolution / morphing tuning ---
EVOLUTION_INTERVAL = 180   # seconds between automatic evolutions
MORPH_STEPS        = 8     # number of intermediate state pushes per transition
MORPH_STEP_DELAY   = 4.0  # seconds between each morph step

DEFAULT_SETTINGS = {
    "bpm": 88,
    "cutoff": 82,
    "synth": "tb303",
    "sleep": 0.5,
}

SYSTEM_PROMPT = """
You are composing evolving lofi/background focus music for Sonic Pi. Output JSON only, no markdown.

Rules:
- Output valid JSON only using schema: version, meta, global, arrangement, parts, fx
- bpm: 72-118. Lofi sweet spot is 75-95. Avoid jarring BPM jumps.
- Always include: kick, hat, bass, pad. Optionally add: snare, lead, texture, perc, arp.
- Prefer 6-8 parts total for richness. More parts = more interesting tracks.
- Part types allowed: drum, bass, chords, melody, texture, perc, arp
- Drum patterns: syncopated, ghost notes, off-beat hats. Not just simple 1/0 grids.
- Bass: 4-8 notes, varied durations. tb303 with res 0.7-0.9 for lofi warmth.
- Pad degrees: at least 4 chords e.g. [1,6,4,5] or [1,3,4,7]. Use invert_chance 0.25-0.4.
- chord_kind: minor7, major7, dom7, minor, major (minor7/major7 for lofi jazz feel)
- arp parts: direction "up"/"down"/"ping_pong", sleep 0.25-0.5, amp 0.10-0.18, probability 0.7-0.9
- perc parts: sparse patterns, probability 0.5-0.75. Use tabla_ke1 or perc_snap samples.
- lead/melody: density 0.3-0.55, sleep_choices like [0.25, 0.5, 0.5, 0.75], repeat_bias 0.2-0.35
- texture: ambi samples, sleep 8-16, amp 0.10-0.20, probability 0.4-0.6
- Amp targets: drums 0.5-0.9, bass 0.3-0.5, pads 0.15-0.28, leads/arps 0.10-0.22
- Allowed scales: minor_pentatonic, major_pentatonic, minor, major, dorian, mixolydian
- Allowed synths: beep, sine, tri, pulse, fm, prophet, tb303, blade, dsaw, supersaw, hollow
- Allowed samples: bd_haus, drum_snare_soft, drum_cymbal_closed, elec_blip, elec_tick, perc_snap, tabla_ke1, ambi_soft_buzz, ambi_lunar_land, guit_em9
- Allowed fx: reverb, echo, lpf, hpf, ixi_techno, slicer, distortion, wobble, krush
- Add at least one fx: reverb mix 0.2-0.35 room 0.6-0.85. Echo mix 0.08-0.15 is a nice pair.
- For authentic lofi: dorian/minor scale, jazzy chord degrees, tabla/perc accents, blade leads, ambi textures.
- Never invent fields outside the schema.
""".strip()

ALLOWED_SCALES = {
    "minor_pentatonic", "major_pentatonic", "minor", "major", "dorian", "mixolydian"
}

ALLOWED_SYNTHS = {
    "beep", "sine", "tri", "pulse", "fm", "prophet", "tb303", "blade", "dsaw", "supersaw", "hollow"
}

ALLOWED_SAMPLES = {
    "bd_haus", "drum_snare_soft", "drum_cymbal_closed", "elec_blip", "elec_tick", "perc_snap", "tabla_ke1",
    "ambi_soft_buzz", "ambi_lunar_land", "guit_em9"
}

ALLOWED_FX = {
    "reverb", "echo", "lpf", "hpf", "ixi_techno", "slicer", "distortion", "wobble", "krush"
}

DEFAULT_SPEC = {
    "version": 1,
    "meta": {
        "title": "Late Night Lofi",
        "energy": 0.38,
        "brightness": 0.35,
        "complexity": 0.42,
        "swing": 0.06,
        "seed": 4242
    },
    "global": {
        "bpm": 88,
        "root": "d2",
        "scale": "dorian",
        "bar_beats": 4,
        "master_amp": 0.88
    },
    "arrangement": {
        "section_length_bars": 8,
        "progression": [
            {"name": "main", "bars": 9999, "active_parts": ["kick", "hat", "snare", "bass", "pad", "lead", "arp", "perc", "texture"]}
        ]
    },
    "parts": [
        {
            "name": "kick",
            "type": "drum",
            "sample": "bd_haus",
            "step_sleep": 0.25,
            "pattern": [1,0,0,0,1,0,0,0,1,0,1,0,1,0,0,0],
            "amp": 0.82,
            "probability": 1.0,
            "humanize_timing": 0.005,
            "humanize_amp": 0.04
        },
        {
            "name": "hat",
            "type": "drum",
            "sample": "drum_cymbal_closed",
            "step_sleep": 0.25,
            "pattern": [0,1,0,1,0,1,0,1,0,1,0,1,0,1,1,0],
            "amp": 0.3,
            "probability": 0.88,
            "humanize_timing": 0.012,
            "humanize_amp": 0.08
        },
        {
            "name": "snare",
            "type": "drum",
            "sample": "drum_snare_soft",
            "step_sleep": 0.25,
            "pattern": [0,0,0,0,1,0,0,0,0,0,0,0,1,0,0,0],
            "amp": 0.5,
            "probability": 0.94,
            "humanize_timing": 0.008,
            "humanize_amp": 0.05
        },
        {
            "name": "bass",
            "type": "bass",
            "synth": "tb303",
            "notes": ["d1", "d1", "f1", "g1", "a1", "g1", "f1", "d1"],
            "durations": [0.5, 0.25, 0.5, 0.75, 0.5, 0.25, 0.5, 1.0],
            "release": 0.18,
            "cutoff": 82,
            "res": 0.82,
            "amp": 0.44,
            "play_probability": 0.94,
            "transpose": 0
        },
        {
            "name": "pad",
            "type": "chords",
            "synth": "prophet",
            "degrees": [1, 6, 4, 5, 1, 3, 4, 2],
            "chord_kind": "minor7",
            "sleep": 4,
            "release": 3.5,
            "cutoff": 88,
            "amp": 0.2,
            "invert_chance": 0.3
        },
        {
            "name": "lead",
            "type": "melody",
            "synth": "blade",
            "density": 0.42,
            "octave": 1,
            "sleep_choices": [0.25, 0.25, 0.5, 0.5, 0.75],
            "release_range": [0.08, 0.32],
            "cutoff_range": [78, 112],
            "amp": 0.17,
            "rest_probability": 0.38,
            "repeat_bias": 0.28
        },
        {
            "name": "arp",
            "type": "arp",
            "synth": "tri",
            "octaves": 2,
            "direction": "up",
            "sleep": 0.25,
            "release": 0.14,
            "cutoff": 90,
            "amp": 0.13,
            "probability": 0.75
        },
        {
            "name": "perc",
            "type": "perc",
            "sample": "tabla_ke1",
            "step_sleep": 0.25,
            "pattern": [0,0,0,0,0,0,1,0,0,0,0,0,0,1,0,0],
            "amp": 0.28,
            "probability": 0.65,
            "rate_range": [0.85, 1.15],
            "humanize_timing": 0.01
        },
        {
            "name": "texture",
            "type": "texture",
            "sample": "ambi_soft_buzz",
            "sleep": 10,
            "rate_range": [0.4, 0.9],
            "amp": 0.13,
            "probability": 0.48
        }
    ],
    "fx": {
        "master": [
            {"name": "reverb", "mix": 0.28, "room": 0.72},
            {"name": "echo", "mix": 0.1, "phase": 0.5, "decay": 4}
        ]
    }
}

app = Flask(__name__, template_folder=str(TEMPLATES_DIR))
lock = threading.Lock()
history = deque(maxlen=20)

state = {
    "last_prompt": None,
    "last_generated_code": None,
    "last_error": None,
    "last_sent_at": None,
    "last_settings": DEFAULT_SETTINGS.copy(),
    "last_spec": None,
    "engine_booted": False,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(BRIDGE_LOG),
        logging.StreamHandler()
    ],
)
log = logging.getLogger("ai_bridge")


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def save_state():
    payload = {
        "state": state,
        "history": list(history),
    }
    STATE_FILE.write_text(json.dumps(payload, indent=2))


def load_state():
    if not STATE_FILE.exists():
        return
    try:
        payload = json.loads(STATE_FILE.read_text())
        saved_state = payload.get("state", {})
        saved_history = payload.get("history", [])
        state.update(saved_state)
        history.clear()
        for item in saved_history[:20]:
            history.append(item)
    except Exception as e:
        log.warning("Could not load state: %s", e)


def tail_file(path: Path, lines: int = 80) -> str:
    if not path.exists():
        return f"{path} does not exist yet.\n"
    try:
        data = path.read_text(errors="replace").splitlines()
        return "\n".join(data[-lines:]) + "\n"
    except Exception as e:
        return f"Could not read {path}: {e}\n"


def tcp_check(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def proc_check(pattern: str) -> bool:
    result = subprocess.run(
        ["pgrep", "-fa", pattern],
        capture_output=True,
        text=True
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def sonic_pi_check() -> bool:
    result = subprocess.run(
        ["sonic-pi-tool.py", "check"],
        capture_output=True,
        text=True
    )
    return result.returncode == 0


def run_code(code: str):
    log.info("Running Sonic Pi code:\n%s", code)
    result = subprocess.run(
        ["sonic-pi-tool", "eval-stdin"],
        input=code,
        text=True,
        capture_output=True
    )
    log.info("sonic-pi-tool rc=%s stdout=%r stderr=%r",
             result.returncode, result.stdout, result.stderr)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "sonic-pi-tool failed").strip()
        raise RuntimeError(err)


def stop_all_jobs():
    result = subprocess.run(
        ["sonic-pi-tool.py", "stop"],
        capture_output=True,
        text=True
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "sonic-pi-tool.py stop failed").strip()
        raise RuntimeError(err)


def repair_json(text: str) -> str:
    """
    Best-effort repair of common small-model JSON defects:
      1. Strip markdown fences and leading/trailing noise
      2. Slice to outermost { ... }
      3. Remove JS // line comments and /* block */ comments
      4. Replace Python literals: True/False/None -> true/false/null
      5. Replace single-quoted strings with double-quoted
      6. Remove trailing commas before } or ]
      7. Close any unclosed brackets/braces (truncated output)
    """
    # 1. strip markdown fences
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text.strip())
    text = text.strip()

    # 2. slice to outermost braces
    start = text.find("{")
    if start == -1:
        raise RuntimeError("Model did not return JSON (no opening brace)")
    depth = 0
    end = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text[start:], start):
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"' and not esc:
            in_str = not in_str
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        text = text[start:]
    else:
        text = text[start:end + 1]

    # 3. remove JS comments
    text = re.sub(r'(?<!:)//[^\n]*', '', text)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)

    # 4. Python literals
    text = re.sub(r'\bTrue\b',  'true',  text)
    text = re.sub(r'\bFalse\b', 'false', text)
    text = re.sub(r'\bNone\b',  'null',  text)

    # 5. single-quoted strings -> double-quoted
    def fix_single_quotes(s):
        result = []
        i = 0
        in_dq = False
        while i < len(s):
            c = s[i]
            if c == '\\' and in_dq:
                result.append(c)
                i += 1
                if i < len(s):
                    result.append(s[i])
                    i += 1
                continue
            if c == '"':
                in_dq = not in_dq
                result.append(c)
                i += 1
                continue
            if c == "'" and not in_dq:
                j = i + 1
                content = []
                while j < len(s) and s[j] != "'":
                    if s[j] == '\\' and j + 1 < len(s):
                        content.append(s[j])
                        j += 1
                    content.append(s[j])
                    j += 1
                inner = ''.join(content)
                inner = inner.replace('"', '\\"')
                result.append('"' + inner + '"')
                i = j + 1
                continue
            result.append(c)
            i += 1
        return ''.join(result)

    text = fix_single_quotes(text)

    # 6. trailing commas
    text = re.sub(r',\s*([}\]])', r'\1', text)

    # 7. close unclosed braces/brackets
    stack = []
    in_str = False
    esc = False
    for ch in text:
        if esc:
            esc = False
            continue
        if ch == '\\':
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
        if in_str:
            continue
        if ch in ('{', '['):
            stack.append(ch)
        elif ch == '}':
            if stack and stack[-1] == '{':
                stack.pop()
        elif ch == ']':
            if stack and stack[-1] == '[':
                stack.pop()

    closers = {'{': '}', '[': ']'}
    for opener in reversed(stack):
        text += closers[opener]

    return text


def extract_json(text: str) -> str:
    """Extract and repair JSON from raw LLM output."""
    try:
        repaired = repair_json(text)
        log.debug("Repaired JSON: %s", repaired[:200])
        return repaired
    except Exception as e:
        raise RuntimeError(f"Could not extract JSON from model output: {e}")


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def ruby_sym(name: str) -> str:
    return f":{str(name).strip().lstrip(':')}"


def ruby_array(items):
    out = []
    for x in items:
        if isinstance(x, str):
            if re.match(r"^[a-g](?:s|b)?\d$", x.lower()):
                out.append(ruby_sym(x.lower()))
            else:
                out.append(repr(x))
        elif isinstance(x, (int, float)):
            out.append(str(x))
        else:
            out.append(repr(x))
    return "[" + ", ".join(out) + "]"


def render_master_fx_open(fx_list):
    lines = []
    for fx in fx_list:
        name = ruby_sym(fx["name"])
        params = ", ".join(f"{k}: {repr(v)}" for k, v in fx.items() if k != "name")
        if params:
            lines.append(f"with_fx {name}, {params} do")
        else:
            lines.append(f"with_fx {name} do")
    return lines


def render_master_fx_close(fx_list):
    return ["end" for _ in fx_list]


def render_drum_loop(p):
    pattern = p.get("pattern", [1, 0, 0, 0])
    step_sleep = float(p.get("step_sleep", 0.25))
    probability = float(clamp(float(p.get("probability", 1.0)), 0.0, 1.0))
    amp = float(clamp(float(p.get("amp", 0.5)), 0.0, 1.0))
    sample = ruby_sym(p.get("sample", "bd_haus"))
    humanize_timing = float(clamp(float(p.get("humanize_timing", 0.0)), 0.0, 0.05))
    humanize_amp = float(clamp(float(p.get("humanize_amp", 0.0)), 0.0, 0.3))

    return f"""
live_loop :{p["name"]} do
  vals = (ring {", ".join(str(int(bool(x))) for x in pattern)})
  hit = vals.tick
  if hit == 1 && rand <= {probability}
    sample {sample}, amp: ({amp} + rrand(-{humanize_amp}, {humanize_amp})).clamp(0, 1)
  end
  sleep ({step_sleep} + rrand(-{humanize_timing}, {humanize_timing})).clamp(0.01, 4)
end
""".strip()


def render_perc_loop(p):
    """Render a sparse percussive loop (tabla, snaps, etc.) with per-step probability."""
    pattern = p.get("pattern", [0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0])
    step_sleep = float(p.get("step_sleep", 0.25))
    probability = float(clamp(float(p.get("probability", 0.7)), 0.0, 1.0))
    amp = float(clamp(float(p.get("amp", 0.3)), 0.0, 1.0))
    sample = ruby_sym(p.get("sample", "tabla_ke1"))
    rate_range = p.get("rate_range", [0.85, 1.15])
    humanize_timing = float(clamp(float(p.get("humanize_timing", 0.008)), 0.0, 0.05))

    return f"""
live_loop :{p["name"]} do
  vals = (ring {", ".join(str(int(bool(x))) for x in pattern)})
  hit = vals.tick
  if hit == 1 && rand <= {probability}
    sample {sample}, rate: rrand({float(rate_range[0])}, {float(rate_range[1])}), amp: {amp}
  end
  sleep ({step_sleep} + rrand(-{humanize_timing}, {humanize_timing})).clamp(0.01, 4)
end
""".strip()


def render_arp_loop(p, root, scale_name):
    """Render an arpeggio loop. direction: up, down, or ping_pong."""
    synth = ruby_sym(p.get("synth", "tri"))
    octaves = int(clamp(int(p.get("octaves", 2)), 1, 4))
    direction = str(p.get("direction", "up"))
    sleep_val = float(clamp(float(p.get("sleep", 0.25)), 0.125, 2.0))
    release = float(clamp(float(p.get("release", 0.15)), 0.05, 2.0))
    cutoff = int(clamp(int(p.get("cutoff", 90)), 40, 130))
    amp = float(clamp(float(p.get("amp", 0.14)), 0.0, 1.0))
    probability = float(clamp(float(p.get("probability", 0.8)), 0.0, 1.0))

    if direction == "down":
        sort_expr = "ns = scale({root}, {scale}, num_octaves: {oct}).sort.reverse".format(
            root=ruby_sym(root), scale=ruby_sym(scale_name), oct=octaves)
        tick_expr = "ns.tick"
    elif direction == "ping_pong":
        sort_expr = (
            "_up = scale({root}, {scale}, num_octaves: {oct}).sort\n"
            "  _dn = _up.reverse\n"
            "  ns = (_up + _dn[1..-2]).ring"
        ).format(root=ruby_sym(root), scale=ruby_sym(scale_name), oct=octaves)
        tick_expr = "ns.tick"
    else:  # up (default)
        sort_expr = "ns = scale({root}, {scale}, num_octaves: {oct}).sort".format(
            root=ruby_sym(root), scale=ruby_sym(scale_name), oct=octaves)
        tick_expr = "ns.tick"

    return f"""
live_loop :{p["name"]} do
  use_synth {synth}
  {sort_expr}
  n = {tick_expr}
  if rand <= {probability}
    play n, release: {release}, cutoff: {cutoff}, amp: {amp}, pan: rrand(-0.35, 0.35)
  end
  sleep {sleep_val}
end
""".strip()


def render_bass_loop(p, root, scale_name):
    notes = p.get("notes", [root])
    durations = p.get("durations", [0.5, 0.5, 1.0])
    synth = ruby_sym(p.get("synth", "tb303"))
    release = float(p.get("release", 0.2))
    cutoff = int(clamp(int(p.get("cutoff", 80)), 40, 130))
    res = float(clamp(float(p.get("res", 0.8)), 0.0, 1.0))
    amp = float(clamp(float(p.get("amp", 0.4)), 0.0, 1.0))
    prob = float(clamp(float(p.get("play_probability", 0.95)), 0.0, 1.0))
    transpose = int(p.get("transpose", 0))

    return f"""
live_loop :{p["name"]} do
  use_synth {synth}
  ns = (ring {", ".join(ruby_sym(n) if isinstance(n, str) else str(n) for n in notes)})
  ds = (ring {", ".join(str(float(d)) for d in durations)})
  n = ns.tick(:n)
  d = ds.tick(:d)
  if rand <= {prob}
    play note(n) + {transpose}, release: {release}, cutoff: {cutoff}, res: {res}, amp: {amp}
  end
  sleep d
end
""".strip()


def render_chord_loop(p, root, scale_name):
    synth = ruby_sym(p.get("synth", "prophet"))
    degrees = p.get("degrees", [1, 4, 6, 5])
    sleep_val = float(p.get("sleep", 4))
    release = float(p.get("release", 3.0))
    cutoff = int(clamp(int(p.get("cutoff", 90)), 40, 130))
    amp = float(clamp(float(p.get("amp", 0.25)), 0.0, 1.0))
    # invert_chance: randomly rotate the chord voicing for harmonic variety
    invert_chance = float(clamp(float(p.get("invert_chance", 0.0)), 0.0, 1.0))

    return f"""
live_loop :{p["name"]} do
  use_synth {synth}
  degs = (ring {", ".join(str(int(d)) for d in degrees)})
  d = degs.tick
  ch = chord_degree(d, {ruby_sym(root)}, {ruby_sym(scale_name)}, 4)
  inv = (rand < {invert_chance}) ? rand_i(3) : 0
  play ch.rotate(inv), release: {release}, cutoff: {cutoff}, amp: {amp}
  sleep {sleep_val}
end
""".strip()


def render_melody_loop(p, root, scale_name):
    synth = ruby_sym(p.get("synth", "blade"))
    octave = int(p.get("octave", 1))
    density = float(clamp(float(p.get("density", 0.4)), 0.0, 1.0))
    sleep_choices = p.get("sleep_choices", [0.25, 0.5, 0.75])
    release_range = p.get("release_range", [0.1, 0.3])
    cutoff_range = p.get("cutoff_range", [80, 110])
    amp = float(clamp(float(p.get("amp", 0.2)), 0.0, 1.0))
    rest_probability = float(clamp(float(p.get("rest_probability", 0.35)), 0.0, 1.0))
    # repeat_bias: chance to replay the last note for lofi phrase repetition
    repeat_bias = float(clamp(float(p.get("repeat_bias", 0.0)), 0.0, 1.0))
    loop_name = p["name"]

    return f"""
live_loop :{loop_name} do
  use_synth {synth}
  ns = scale({ruby_sym(root)}, {ruby_sym(scale_name)}, num_octaves: {max(1, octave + 1)})
  if rand > {rest_probability}
    n = (rand < {repeat_bias} && @{loop_name}_last) ? @{loop_name}_last : ns.choose
    @{loop_name}_last = n
    play n,
      release: rrand({float(release_range[0])}, {float(release_range[1])}),
      cutoff: rrand_i({int(cutoff_range[0])}, {int(cutoff_range[1])}),
      amp: {amp * max(0.2, density)},
      pan: rrand(-0.4, 0.4)
  end
  sleep (ring {", ".join(str(float(x)) for x in sleep_choices)}).choose
end
""".strip()


def render_texture_loop(p):
    sample = ruby_sym(p.get("sample", "ambi_soft_buzz"))
    sleep_val = float(p.get("sleep", 8))
    rate_range = p.get("rate_range", [0.5, 1.0])
    amp = float(clamp(float(p.get("amp", 0.2)), 0.0, 1.0))
    prob = float(clamp(float(p.get("probability", 0.5)), 0.0, 1.0))

    return f"""
live_loop :{p["name"]} do
  if rand <= {prob}
    sample {sample}, rate: rrand({float(rate_range[0])}, {float(rate_range[1])}), amp: {amp}
  end
  sleep {sleep_val}
end
""".strip()


def find_part(spec: dict, name: str):
    for p in spec.get("parts", []):
        if p.get("name") == name:
            return p
    return None


def render_sonic_pi_code(spec: dict) -> str:
    """
    Single source of truth for all Sonic Pi code generation.
    Used for both initial boot and live updates — Sonic Pi hot-swaps
    live_loops with the same name at the next safe loop boundary.
    """
    g = spec["global"]
    fx_list = spec.get("fx", {}).get("master", []) if isinstance(spec.get("fx"), dict) else []
    lines = [
        f"use_bpm {int(g['bpm'])}",
        f"use_random_seed {int(spec['meta']['seed'])}",
        f"set_volume! {float(g['master_amp'])}",
        ""
    ]

    lines += render_master_fx_open(fx_list)

    for p in spec["parts"]:
        ptype = p.get("type")
        if ptype == "drum":
            lines.append(render_drum_loop(p))
        elif ptype == "perc":
            lines.append(render_perc_loop(p))
        elif ptype == "bass":
            lines.append(render_bass_loop(p, g["root"], g["scale"]))
        elif ptype == "chords":
            lines.append(render_chord_loop(p, g["root"], g["scale"]))
        elif ptype == "melody":
            lines.append(render_melody_loop(p, g["root"], g["scale"]))
        elif ptype == "arp":
            lines.append(render_arp_loop(p, g["root"], g["scale"]))
        elif ptype == "texture":
            lines.append(render_texture_loop(p))
        else:
            log.warning("Unknown part type %r for part %r — skipped", ptype, p.get("name"))

        lines.append("")

    lines += render_master_fx_close(fx_list)
    return "\n".join(lines).strip()


def validate_spec(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise RuntimeError("Spec is not a JSON object")

    spec.setdefault("version", 1)
    spec.setdefault("meta", {})
    spec.setdefault("global", {})
    spec.setdefault("arrangement", {})
    spec.setdefault("parts", [])
    spec.setdefault("fx", {})

    # Coerce top-level fields that must be dicts (model occasionally returns lists)
    if not isinstance(spec["fx"], dict):
        spec["fx"] = {}
    if not isinstance(spec["arrangement"], dict):
        spec["arrangement"] = {}

    g = spec["global"]
    g["bpm"] = int(clamp(int(g.get("bpm", state["last_settings"]["bpm"])), 70, 140))
    g["root"] = str(g.get("root", "d2")).lower()
    g["scale"] = str(g.get("scale", "dorian"))
    if g["scale"] not in ALLOWED_SCALES:
        g["scale"] = "dorian"
    g["bar_beats"] = int(clamp(int(g.get("bar_beats", 4)), 3, 8))
    g["master_amp"] = float(clamp(float(g.get("master_amp", 0.88)), 0.0, 1.0))

    meta = spec["meta"]
    meta["title"] = str(meta.get("title", "AI Piece"))[:120]
    meta["energy"] = float(clamp(float(meta.get("energy", 0.4)), 0.0, 1.0))
    meta["brightness"] = float(clamp(float(meta.get("brightness", 0.4)), 0.0, 1.0))
    meta["complexity"] = float(clamp(float(meta.get("complexity", 0.35)), 0.0, 1.0))
    meta["swing"] = float(clamp(float(meta.get("swing", 0.0)), 0.0, 0.2))
    meta["seed"] = int(meta.get("seed", random.randint(1, 999999)))

    arr = spec["arrangement"]
    prog = arr.get("progression", [])
    if not isinstance(prog, list) or not prog:
        arr["progression"] = [{"name": "main", "bars": 9999, "active_parts": []}]

    VALID_TYPES = {"drum", "perc", "bass", "chords", "melody", "arp", "texture"}
    valid_parts = []
    for p in spec["parts"]:
        if not isinstance(p, dict):
            continue
        p["name"] = str(p.get("name", f"part_{len(valid_parts)}")).lower()
        p["type"] = str(p.get("type", "texture")).lower()
        if p["type"] not in VALID_TYPES:
            log.warning("Dropping part %r with unknown type %r", p["name"], p["type"])
            continue
        p["amp"] = float(clamp(float(p.get("amp", 0.4)), 0.0, 1.0))

        if "synth" in p:
            p["synth"] = str(p["synth"]).lower().lstrip(":")
            if p["synth"] not in ALLOWED_SYNTHS:
                p["synth"] = "sine"

        if "sample" in p:
            p["sample"] = str(p["sample"]).lower().lstrip(":")
            if p["sample"] not in ALLOWED_SAMPLES:
                p["sample"] = "elec_blip"

        valid_parts.append(p)

    if not valid_parts:
        return DEFAULT_SPEC.copy()

    spec["parts"] = valid_parts

    master_fx = spec["fx"].get("master", [])
    cleaned_fx = []
    for f in master_fx:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name", "")).lower()
        if name in ALLOWED_FX:
            cleaned_fx.append(f)
    spec["fx"]["master"] = cleaned_fx[:4]

    return spec


def send_settings(bpm, cutoff, synth, sleep):
    bpm = int(bpm)
    cutoff = int(cutoff)
    synth = str(synth).strip().lstrip(":")
    sleep = float(sleep)

    with lock:
        state["last_settings"] = {
            "bpm": bpm,
            "cutoff": cutoff,
            "synth": synth,
            "sleep": sleep,
        }
        save_state()

    log.info("Saved settings bpm=%s cutoff=%s synth=%s sleep=%s", bpm, cutoff, synth, sleep)
    return state["last_settings"]


def lerp_val(a, b, t):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a + (b - a) * t
    return b


def lerp_spec(s1: dict, s2: dict, t: float) -> dict:
    """Blend numeric params between s1 and s2 at position t (0..1) for smooth morphing."""
    merged = copy.deepcopy(s2)

    # Global params
    g1 = s1.get("global", {})
    g2 = s2.get("global", {})
    merged["global"]["bpm"] = int(lerp_val(g1.get("bpm", 88), g2.get("bpm", 88), t))
    merged["global"]["master_amp"] = round(
        lerp_val(g1.get("master_amp", 0.88), g2.get("master_amp", 0.88), t), 3)

    # Per-part numeric fields that benefit from smooth interpolation
    LERP_FIELDS = ["amp", "density", "cutoff", "release", "res", "probability"]
    for p2 in merged.get("parts", []):
        p1 = find_part(s1, p2["name"])
        if not p1:
            continue
        for field in LERP_FIELDS:
            if field in p1 and field in p2:
                raw = lerp_val(p1[field], p2[field], t)
                p2[field] = int(round(raw)) if field == "cutoff" else round(raw, 3)

    return merged


def morph_to_spec(old_spec: dict, new_spec: dict):
    """Push MORPH_STEPS intermediate renders to Sonic Pi for a smooth transition."""
    for i in range(1, MORPH_STEPS + 1):
        t = i / MORPH_STEPS
        intermediate = lerp_spec(old_spec, new_spec, t)
        try:
            run_code(render_sonic_pi_code(intermediate))
            log.info("Morph step %d/%d (t=%.2f)", i, MORPH_STEPS, t)
        except Exception as e:
            log.warning("Morph step %d failed: %s", i, e)
            break
        if i < MORPH_STEPS:
            time.sleep(MORPH_STEP_DELAY)


def generate_and_send(prompt_text: str):
    settings = state["last_settings"]

    payload = {
        "model": MODEL,
        "prompt": (
            f"User request: {prompt_text}\n"
            f"Current preferences: bpm={settings['bpm']}, cutoff={settings['cutoff']}, "
            f"synth={settings['synth']}, sleep={settings['sleep']}\n"
            f"Return a JSON composition spec only."
        ),
        "system": SYSTEM_PROMPT,
        "stream": False,
        "options": {
            "temperature": 0.85,
        },
    }

    log.info("Generating music spec for prompt: %s", prompt_text)

    response = requests.post(OLLAMA_URL, json=payload, timeout=60)
    response.raise_for_status()
    raw = response.json().get("response", "").strip()

    if not raw:
        raise RuntimeError("Ollama returned empty response")

    log.debug("Raw model output: %s", raw[:500])
    raw_json = extract_json(raw)
    spec = json.loads(raw_json)
    spec = validate_spec(spec)

    code = render_sonic_pi_code(spec)

    with lock:
        booted = state.get("engine_booted", False)
        old_spec = state.get("last_spec") or spec

    if not booted:
        # First boot: send full code directly
        run_code(code)
    else:
        # Subsequent updates: morph smoothly, final step sends the full new code
        morph_to_spec(old_spec, spec)

    item = {
        "time": now(),
        "prompt": prompt_text,
        "spec": spec,
        "code": code,
    }

    with lock:
        history.appendleft(item)
        state["last_prompt"] = prompt_text
        state["last_generated_code"] = code
        state["last_error"] = None
        state["last_sent_at"] = item["time"]
        state["engine_booted"] = True
        state["last_spec"] = spec
        save_state()

    log.info("Rendered and sent Sonic Pi code")
    return item


def send_test_ping():
    spec = DEFAULT_SPEC.copy()
    code = render_sonic_pi_code(spec)
    run_code(code)

    with lock:
        state["engine_booted"] = True
        if not state.get("last_spec"):
            state["last_spec"] = spec
        save_state()

    log.info("Sent test composition")
    return {"status": "ok", "message": "Test composition sent"}


def evolution_loop():
    """Background thread: auto-evolve the music every EVOLUTION_INTERVAL seconds."""
    time.sleep(EVOLUTION_INTERVAL)
    while True:
        try:
            with lock:
                booted = state.get("engine_booted", False)
                last_spec = state.get("last_spec")
            if booted and last_spec:
                title = last_spec.get("meta", {}).get("title", "current piece")
                prompt = (
                    f"Subtly evolve '{title}'. Keep the same mood but shift one or two "
                    f"elements: adjust rhythm density, transpose the bass slightly, alter "
                    f"pad degrees, or gently brighten/darken the texture. Stay subtle."
                )
                log.info("Auto-evolution: triggering")
                generate_and_send(prompt)
                log.info("Auto-evolution: complete")
        except Exception as e:
            log.warning("Auto-evolution failed: %s", e)
        time.sleep(EVOLUTION_INTERVAL)


def status_payload():
    return {
        "time": now(),
        "ollama_up": tcp_check("127.0.0.1", 11434),
        "webui_up": True,
        "sonic_pi_process": proc_check("sonic-pi"),
        "sonic_pi_server_up": sonic_pi_check(),
        "jackd_process": proc_check("jackd"),
        "ffmpeg_process": proc_check("ffmpeg|srt-live-transmit"),
        "xvfb_process": proc_check("Xvfb"),
        "last_prompt": state.get("last_prompt"),
        "last_sent_at": state.get("last_sent_at"),
        "last_error": state.get("last_error"),
        "settings": state.get("last_settings"),
        "engine_booted": state.get("engine_booted", False),
        "evolution_interval_sec": EVOLUTION_INTERVAL,
    }


@app.route("/")
def index():
    return render_template("index.html", model=MODEL, settings=state["last_settings"])


@app.route("/api/status")
def api_status():
    return jsonify(status_payload())


@app.route("/api/history")
def api_history():
    return jsonify(list(history))


@app.route("/api/logs")
def api_logs():
    return jsonify({
        "bridge": tail_file(BRIDGE_LOG, 120),
        "headless": tail_file(HEADLESS_LOG, 120),
    })


@app.route("/api/settings", methods=["POST"])
def api_settings():
    data = request.get_json(force=True)
    updated = send_settings(
        data.get("bpm", DEFAULT_SETTINGS["bpm"]),
        data.get("cutoff", DEFAULT_SETTINGS["cutoff"]),
        data.get("synth", DEFAULT_SETTINGS["synth"]),
        data.get("sleep", DEFAULT_SETTINGS["sleep"]),
    )
    return jsonify({"status": "ok", "settings": updated})


@app.route("/api/generate", methods=["POST"])
def api_generate():
    data = request.get_json(force=True)
    prompt_text = (data.get("prompt") or "").strip()
    if not prompt_text:
        return jsonify({"status": "error", "message": "Prompt is required"}), 400

    try:
        item = generate_and_send(prompt_text)
        return jsonify({"status": "ok", "item": item})
    except Exception as e:
        msg = str(e)
        with lock:
            state["last_error"] = msg
            save_state()
        log.exception("Generation failed")
        return jsonify({"status": "error", "message": msg}), 500


@app.route("/api/test-ping", methods=["POST"])
def api_test_ping():
    try:
        return jsonify(send_test_ping())
    except Exception as e:
        msg = str(e)
        with lock:
            state["last_error"] = msg
            save_state()
        log.exception("Ping failed")
        return jsonify({"status": "error", "message": msg}), 500


@app.route("/api/stop", methods=["POST"])
def api_stop():
    try:
        stop_all_jobs()
        return jsonify({"status": "ok", "message": "Stopped Sonic Pi jobs"})
    except Exception as e:
        msg = str(e)
        with lock:
            state["last_error"] = msg
            save_state()
        log.exception("Stop failed")
        return jsonify({"status": "error", "message": msg}), 500


if __name__ == "__main__":
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    (BASE_DIR / "logs").mkdir(parents=True, exist_ok=True)
    load_state()
    log.info("Starting AI bridge web UI on port %s", WEB_PORT)
    send_settings(**DEFAULT_SETTINGS)
    threading.Thread(target=evolution_loop, daemon=True).start()
    log.info("Auto-evolution thread started (interval: %ds, morph steps: %d x %.1fs)",
             EVOLUTION_INTERVAL, MORPH_STEPS, MORPH_STEP_DELAY)
    app.run(host="0.0.0.0", port=WEB_PORT, debug=False)
