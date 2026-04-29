#!/usr/bin/env python3

import random
import json
import logging
import re
import socket
import subprocess
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request

BASE_DIR = Path("/root/music")
TEMPLATES_DIR = BASE_DIR / "templates"
BRIDGE_LOG = BASE_DIR / "sonic-ai-bridge.log"
HEADLESS_LOG = BASE_DIR / "sonic-pi-headless.log"
STATE_FILE = BASE_DIR / "ai_state.json"

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
WEB_PORT = 8081
MODEL = "qwen2.5:1.5b"

DEFAULT_SETTINGS = {
    "bpm": 100,
    "cutoff": 70,
    "synth": "supersaw",
    "sleep": 0.5,
}

SYSTEM_PROMPT = """
You are composing music for Sonic Pi. Output JSON only, with no markdown and no explanations.

Your job is to create a rich music specification that matches the intent of your prompt, always adhering to a base that is pleasant listening, not distracting, background focus music.

Rules:
- Output valid JSON only.
- Use this schema:
  version, meta, global, arrangement, parts, fx
- Keep bpm between 70 and 140.
- Allowed scales:
  minor_pentatonic, major_pentatonic, minor, major, dorian, mixolydian
- Allowed drum samples:
  bd_haus, drum_snare_soft, drum_cymbal_closed, elec_blip, elec_tick, perc_snap, tabla_ke1
- Allowed synths:
  beep, sine, tri, pulse, fm, prophet, tb303, blade, dsaw, supersaw, hollow
- Allowed part types:
  drum, bass, chords, melody, texture
- Keep amp values between 0.0 and 1.0.
- Keep it musical, varied, and suitable for focus listening unless the user asks otherwise.
- Always include at least: kick, hat, bass, chords or pad.
- Prefer 4 to 7 parts total.
- Use small note sets and clear rhythmic patterns.
- Never invent fields outside the schema unless they are simple numeric or list variations of existing concepts.
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
        "title": "Default Focus Groove",
        "energy": 0.4,
        "brightness": 0.4,
        "complexity": 0.35,
        "swing": 0.04,
        "seed": 1234
    },
    "global": {
        "bpm": 100,
        "root": "e2",
        "scale": "minor_pentatonic",
        "bar_beats": 4,
        "master_amp": 0.9
    },
    "arrangement": {
        "section_length_bars": 8,
        "progression": [
            {"name": "main", "bars": 9999, "active_parts": ["kick", "hat", "bass", "pad"]}
        ]
    },
    "parts": [
        {
            "name": "kick",
            "type": "drum",
            "sample": "bd_haus",
            "step_sleep": 0.5,
            "pattern": [1,0,1,0,1,0,1,0],
            "amp": 0.9,
            "probability": 1.0,
            "humanize_timing": 0.0,
            "humanize_amp": 0.03
        },
        {
            "name": "hat",
            "type": "drum",
            "sample": "drum_cymbal_closed",
            "step_sleep": 0.25,
            "pattern": [0,0,1,0,0,0,1,0,0,0,1,0,0,0,1,0],
            "amp": 0.35,
            "probability": 0.9,
            "humanize_timing": 0.01,
            "humanize_amp": 0.06
        },
        {
            "name": "bass",
            "type": "bass",
            "synth": "tb303",
            "notes": ["e1", "e1", "g1", "a1"],
            "durations": [0.5, 0.5, 0.5, 0.5],
            "release": 0.2,
            "cutoff": 80,
            "res": 0.8,
            "amp": 0.45,
            "play_probability": 0.95,
            "transpose": 0
        },
        {
            "name": "pad",
            "type": "chords",
            "synth": "prophet",
            "degrees": [1, 4, 6, 5],
            "chord_kind": "minor7",
            "sleep": 4,
            "release": 3.2,
            "cutoff": 92,
            "amp": 0.22,
            "invert_chance": 0.25
        }
    ],
    "fx": {
        "master": [
            {"name": "reverb", "mix": 0.2, "room": 0.7}
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


def extract_json(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError("Model did not return JSON")
    return text[start:end+1]


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

    return f"""
live_loop :{p["name"]} do
  use_synth {synth}
  degs = (ring {", ".join(str(int(d)) for d in degrees)})
  d = degs.tick
  ch = chord_degree(d, {ruby_sym(root)}, {ruby_sym(scale_name)}, 4)
  play ch, release: {release}, cutoff: {cutoff}, amp: {amp}
  sleep {sleep_val}
end
""".strip()

def render_engine_boot(spec: dict) -> str:
    g = spec["global"]
    root = ruby_sym(g["root"])
    scale_name = ruby_sym(g["scale"])

    return f"""
set :ai_bpm, {int(g["bpm"])}
set :ai_root, {root}
set :ai_scale, {scale_name}
set :ai_master_amp, {float(g["master_amp"])}

set :ai_kick_pattern, {ruby_array(find_part(spec, "kick").get("pattern", [1,0,0,0]))}
set :ai_hat_pattern, {ruby_array(find_part(spec, "hat").get("pattern", [0,0,1,0]))}
set :ai_snare_pattern, {ruby_array(find_part(spec, "snare").get("pattern", [0,0,0,0,1,0,0,0]))}

set :ai_bass_notes, {ruby_array(find_part(spec, "bass").get("notes", ["e1","g1","a1"]))}
set :ai_bass_durations, {ruby_array(find_part(spec, "bass").get("durations", [0.5,0.5,1.0]))}

set :ai_pad_degrees, {ruby_array(find_part(spec, "pad").get("degrees", [1,4,6,5]))}
set :ai_lead_density, {float(find_part(spec, "lead").get("density", 0.35) if find_part(spec, "lead") else 0.35)}

live_loop :conductor do
  use_bpm get(:ai_bpm)
  sleep 1
end

live_loop :kick do
  use_bpm get(:ai_bpm)
  pat = (ring *get(:ai_kick_pattern))
  sample :bd_haus, amp: 0.9 * get(:ai_master_amp) if pat.tick == 1
  sleep 0.25
end

live_loop :hat do
  use_bpm get(:ai_bpm)
  pat = (ring *get(:ai_hat_pattern))
  sample :drum_cymbal_closed, amp: 0.35 * get(:ai_master_amp) if pat.tick == 1
  sleep 0.25
end

live_loop :snare do
  use_bpm get(:ai_bpm)
  pat = (ring *get(:ai_snare_pattern))
  sample :drum_snare_soft, amp: 0.5 * get(:ai_master_amp) if pat.tick == 1
  sleep 0.25
end

live_loop :bass do
  use_bpm get(:ai_bpm)
  use_synth :tb303
  ns = (ring *get(:ai_bass_notes))
  ds = (ring *get(:ai_bass_durations))
  play note(ns.tick), release: 0.2, cutoff: 85, res: 0.8, amp: 0.45 * get(:ai_master_amp)
  sleep ds.tick
end

live_loop :pad do
  use_bpm get(:ai_bpm)
  use_synth :prophet
  degs = (ring *get(:ai_pad_degrees))
  ch = chord_degree(degs.tick, get(:ai_root), get(:ai_scale), 4)
  play ch, sustain: 3, release: 1, cutoff: 90, amp: 0.22 * get(:ai_master_amp)
  sleep 4
end

live_loop :lead do
  use_bpm get(:ai_bpm)
  use_synth :blade
  ns = scale(get(:ai_root), get(:ai_scale), num_octaves: 2)
  if rand < get(:ai_lead_density)
    play ns.choose, release: rrand(0.1, 0.3), cutoff: rrand_i(80, 110), amp: 0.18 * get(:ai_master_amp)
  end
  sleep [0.25, 0.5, 0.75].choose
end
""".strip()

def render_state_update(spec: dict) -> str:
    g = spec["global"]
    kick = find_part(spec, "kick")
    hat = find_part(spec, "hat")
    snare = find_part(spec, "snare")
    bass = find_part(spec, "bass")
    pad = find_part(spec, "pad")
    lead = find_part(spec, "lead")

    return f"""
set :ai_bpm, {int(g["bpm"])}
set :ai_root, {ruby_sym(g["root"])}
set :ai_scale, {ruby_sym(g["scale"])}
set :ai_master_amp, {float(g["master_amp"])}

set :ai_kick_pattern, {ruby_array(kick.get("pattern", [1,0,0,0])) if kick else "[1,0,0,0]"}
set :ai_hat_pattern, {ruby_array(hat.get("pattern", [0,0,1,0])) if hat else "[0,0,1,0]"}
set :ai_snare_pattern, {ruby_array(snare.get("pattern", [0,0,0,0,1,0,0,0])) if snare else "[0,0,0,0,1,0,0,0]"}

set :ai_bass_notes, {ruby_array(bass.get("notes", ["e1","g1","a1"])) if bass else "[:e1,:g1,:a1]"}
set :ai_bass_durations, {ruby_array(bass.get("durations", [0.5,0.5,1.0])) if bass else "[0.5,0.5,1.0]"}

set :ai_pad_degrees, {ruby_array(pad.get("degrees", [1,4,6,5])) if pad else "[1,4,6,5]"}
set :ai_lead_density, {float(lead.get("density", 0.35) if lead else 0.35)}
""".strip()

def find_part(spec: dict, name: str):
    for p in spec.get("parts", []):
        if p.get("name") == name:
            return p
    return None

def render_melody_loop(p, root, scale_name):
    synth = ruby_sym(p.get("synth", "blade"))
    octave = int(p.get("octave", 1))
    density = float(clamp(float(p.get("density", 0.4)), 0.0, 1.0))
    sleep_choices = p.get("sleep_choices", [0.25, 0.5, 0.75])
    release_range = p.get("release_range", [0.1, 0.3])
    cutoff_range = p.get("cutoff_range", [80, 110])
    amp = float(clamp(float(p.get("amp", 0.2)), 0.0, 1.0))
    rest_probability = float(clamp(float(p.get("rest_probability", 0.35)), 0.0, 1.0))

    return f"""
live_loop :{p["name"]} do
  use_synth {synth}
  ns = scale({ruby_sym(root)}, {ruby_sym(scale_name)}, num_octaves: {max(1, octave + 1)})
  if rand > {rest_probability}
    play ns.choose,
      release: rrand({float(release_range[0])}, {float(release_range[1])}),
      cutoff: rrand_i({int(cutoff_range[0])}, {int(cutoff_range[1])}),
      amp: {amp * max(0.2, density)}
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


def render_sonic_pi_code(spec: dict) -> str:
    g = spec["global"]
    fx_list = spec.get("fx", {}).get("master", [])
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
        elif ptype == "bass":
            lines.append(render_bass_loop(p, g["root"], g["scale"]))
        elif ptype == "chords":
            lines.append(render_chord_loop(p, g["root"], g["scale"]))
        elif ptype == "melody":
            lines.append(render_melody_loop(p, g["root"], g["scale"]))
        elif ptype == "texture":
            lines.append(render_texture_loop(p))

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

    g = spec["global"]
    g["bpm"] = int(clamp(int(g.get("bpm", state["last_settings"]["bpm"])), 70, 140))
    g["root"] = str(g.get("root", "e2")).lower()
    g["scale"] = str(g.get("scale", "minor_pentatonic"))
    if g["scale"] not in ALLOWED_SCALES:
        g["scale"] = "minor_pentatonic"
    g["bar_beats"] = int(clamp(int(g.get("bar_beats", 4)), 3, 8))
    g["master_amp"] = float(clamp(float(g.get("master_amp", 0.9)), 0.0, 1.0))

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

    valid_parts = []
    for p in spec["parts"]:
        if not isinstance(p, dict):
            continue
        p["name"] = str(p.get("name", f"part_{len(valid_parts)}")).lower()
        p["type"] = str(p.get("type", "texture")).lower()
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

    fx = spec.get("fx", {})
    master_fx = fx.get("master", [])
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
            "temperature": 0.8,
        },
    }

    log.info("Generating music spec for prompt: %s", prompt_text)

    response = requests.post(OLLAMA_URL, json=payload, timeout=60)
    response.raise_for_status()
    raw = response.json().get("response", "").strip()

    if not raw:
        raise RuntimeError("Ollama returned empty response")

raw_json = extract_json(raw)
spec = json.loads(raw_json)
spec = validate_spec(spec)

with lock:
    booted = state.get("engine_booted", False)

if not booted:
    code = render_engine_boot(spec)
else:
    code = render_state_update(spec)

run_code(code)

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
    save_state()

    log.info("Rendered and sent Sonic Pi code")
    return item

def send_test_ping():
    spec = DEFAULT_SPEC.copy()
    with lock:
        booted = state.get("engine_booted", False)

    code = render_engine_boot(spec) if not booted else render_state_update(spec)
    run_code(code)

    with lock:
        state["engine_booted"] = True
        save_state()

    log.info("Sent test composition update")
    return {"status": "ok", "message": "Test composition sent"}
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
    load_state()
    log.info("Starting AI bridge web UI on port %s", WEB_PORT)
    send_settings(**DEFAULT_SETTINGS)
    app.run(host="0.0.0.0", port=WEB_PORT, debug=False)
