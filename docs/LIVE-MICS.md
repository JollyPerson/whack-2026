# Live microphones: one phone and one USB-C mic

This is the operator guide for the live demo. It assumes:
- one **Android phone** used as Prop's microphone, through **MicYou** over a USB cable;
- one **USB-C microphone** for Opp;
- the ThinkPad running the server, the ears, the two avatars and the dashboard.

It covers setup, checks, the run order, what to expect, what the ears terminal tells you, and what to do when
something breaks mid-demo. The general runbook is [`SIDE-BOTS.md`](SIDE-BOTS.md); this guide adds the microphone
detail.

> **What was verified, and what wasn't.** No phone and no USB-C mic were attached while this was written. The laptop
> commands and their output below were run on this laptop (ThinkPad T14 Gen1, Arch, PipeWire 1.6.10, WirePlumber
> 0.5.18). MicYou's virtual devices were recreated with the exact `pw-cli` and `pw-loopback` commands from its source
> code, and the failure behaviour was tested on those copies and on null sinks loaded and unloaded mid-run. The MicYou
> app itself, a real phone and a real USB-C mic have **not** been tested. Anything marked **(untested)** needs a
> rehearsal.

## 1. How the audio gets in

```
phone mic --USB/adb--> MicYou desktop --> PipeWire source "MicYouVirtualSink.monitor" --+
                                                                                        +--> parec x2 --> ears/ears.py (Whisper)
USB-C mic --------------------------------> PipeWire source "alsa_input.usb-..." --------+          --> ears/bridge.py --> server
```

- No MicYou? The server's own phone page does the same job from the phone's browser, with no app: section 12.
- `scripts/ears-two-mics.sh` runs `ears/ears.py` with `--source mic1=$MIC1 --source mic2=$MIC2`. It pipes the
  sentences into `ears/bridge.py`, which posts them to `$SERVER/publish` (default `http://127.0.0.1:8000/publish`).
  Every sentence is also written to `logs/ears-<date>-<time>.jsonl`.
- **mic1 is speaker A (Prop)** and **mic2 is speaker B (Opp)**. This guide uses MIC1 = phone and MIC2 = USB-C mic,
  the same mapping as teammate A's `ears/runstt.sh`.
- Each source is captured with
  `parec --device <name> --format=s16le --rate=16000 --channels=1 --raw --property=node.dont-reconnect=true`.
  PipeWire resamples and downmixes to 16 kHz mono, so any device rate (48 kHz stereo, say) is fine. On a stereo mic
  with sound on one channel only, the downmix is 6 dB quieter. That's harmless, but set the gain with the meter below,
  which hears exactly what ears hears.
- parec hands audio over in **2 s bursts** (measured on this laptop). That is part of the delay in section 7.

### A missing mic: what PipeWire does, and what ears does about it
PipeWire's own behaviour, tested on this laptop:
- `parec -d nope_not_a_source` records the **default microphone** without any error.
- When a source disappears mid-run (USB mic unplugged, MicYou desktop closed), a plain parec stream **moves** to the
  default source, or for a `.monitor` to the default sink's monitor (whatever the laptop plays). It never logs a thing.

What the merged ears and launcher do instead:
- **The launcher checks the names before it starts** (section 5): it refuses MIC1 = MIC2, a name that isn't a current
  source, and the monitor of the laptop's speakers or HDMI.
- **ears looks the source up before every start and restart of parec.** If the name isn't in
  `pactl list short sources`, ears doesn't start parec: the mic stays **deaf** and says so, and ears keeps retrying.
- **parec runs with `node.dont-reconnect`**, so when its source vanishes mid-run it exits instead of moving. ears
  restarts it with backoff (0.5 s, 1 s, then every 2 s) and picks the source up again when the **same name** is back.
- So a dropped mic records **nothing**. It never quietly transcribes the laptop mic or the speakers, and it never
  switches to another source on its own.
- **Which source is the default doesn't matter.** ears records the default source normally, so it's fine if PipeWire
  makes the USB mic the default when it's plugged in, or if MicYou makes its own mic the default. No silent virtual
  default source is needed any more.

(`--property=node.dont-fallback=true` isn't used: with it, parec could not record the default source at all on this
laptop, "Stream error: Entity killed", and a freshly plugged USB mic is often the default.)

## 2. Hardware and placement

| Item | Notes |
|---|---|
| Android phone + **data** USB cable | A USB-A to C cable into the laptop's USB-A port leaves both USB-C ports free. Some cables are charge-only and adb won't see the phone; bring a known data cable. |
| USB-C microphone | It must be a **USB Audio Class** mic: a USB-C lavalier with its own chip, a USB-C desktop mic, or a wireless receiver with USB-C. A passive "analog over USB-C" mic made for phones won't appear on a laptop. Check this in step 2. |
| Laptop speakers or HDMI | Carry the verdict music, and the referee's voice only if a tab uses `audio=1`. |
| Backup | Wired earbuds with an inline mic (TRRS) for the laptop's 3.5 mm combo jack (section 9). |

### Placement
- **One mic per debater, as close as is comfortable.** The phone goes on a stand or in Prop's hand, 10–15 cm from the
  mouth. Point its bottom edge (the main mic) at the chin, slightly off-axis so "p" and "b" sounds don't pop. Don't
  cover the bottom edge with fingers.
- **The USB-C mic** goes 10–20 cm from Opp's mouth. Clip a lavalier on the chest, 15–20 cm below the chin. Point a
  cardioid desk mic at the mouth, with its back (its dead side) towards Prop and the speakers.
- **Keep the debaters at least 2 m apart,** Prop on the left of the screen (the For bot's side) and Opp on the right.
  Each doubling of distance is about 6 dB quieter. A mouth at 15 cm against the other debater at 2 m gives roughly
  20 dB between "my voice" and "their voice" on each mic. That gap is what keeps bleed down.
- **Point the mics apart, and the speakers at the audience,** away from both mics, at a moderate volume. Nothing in this
  setup plays a mic back through the speakers, so there is no feedback loop unless someone adds one (section 9).
- Phone mics are usually omnidirectional, so the phone side bleeds more. Put Prop on the side away from the speakers.

### How the system tolerates bleed
Each mic will hear the other debater, quietly.
- **Whisper's VAD** (`vad_filter=True`) skips audio too quiet to be speech, so a well-separated far voice usually
  never becomes text.
- **The bridge** (`ears/bridge.py`) doesn't let a bleed copy end a turn. A sentence of **4 or more words** whose words
  are at least **70%** the same as the other mic's sentence, with the two overlapping in time (1.5 s of slack), is
  treated as a copy. It's still sent, but it doesn't send the other speaker's `Pause`.
- **The brain's `BleedGuard`** (`app/brain/run.py`) then drops that copy, by the same rule. It keeps the **first one to
  arrive**, which is not necessarily the right speaker. So a loud bleed copy that wins the race can put a claim on the
  wrong side and drop the real sentence.
- A debater who repeats the opponent's words a few seconds **later** is taking a normal turn, not bleed.
- **Short bleed (under 4 words) is never filtered.** The bridge treats it as "the other speaker started": it ends the
  current speaker's turn with an immediate `Pause`, and that lets the referee cut in mid-turn.
- Good placement and gain are therefore still the real defence. The target is **15 dB or more** between the near and
  the far mic (the bleed test in step 6).
- **`EchoGuard`** separately drops the referee's own voice when a mic picks it up. It needs the `audio=1` tab to report
  when it speaks.

## 3. Before the venue: installs that need internet

Do these now, on the laptop and the phone:

```bash
sudo pacman -S android-tools          # adb: NOT installed on this laptop yet (android-udev and usbmuxd already are)
curl -LO https://github.com/MicYou-Dev/MicYou/releases/download/v2.1.1/MicYou-Linux-2.1.1.AppImage
chmod +x MicYou-Linux-2.1.1.AppImage  # fuse2 is installed, so the AppImage runs
```

- MicYou is not installed on this laptop. The AUR also has `micyou-bin`.
- On the phone, install `MicYou-Android-2.1.1.apk` from the same release page:
  <https://github.com/MicYou-Dev/MicYou/releases>.
- Pre-download the `tiny.en` Whisper model (the mid-demo fallback if `base.en` falls behind) while online:
  `cd ears && uv run --frozen python -c "from faster_whisper import WhisperModel; WhisperModel('tiny.en', device='cpu', compute_type='int8')"`
- The Whisper models `base.en`, `small.en` and (after the step above) `tiny.en` are cached, and `ears/.venv` exists. At the venue, start the ears
  with `HF_HUB_OFFLINE=1`, so it never waits on the network to check the model. It loads in about 1 s from the cache.
- **iPhone instead?** MicYou's iOS app must be sideloaded with a Mac or Windows PC and iOS 18+, which isn't realistic
  today. MicYou's **Web mode** (a phone browser, TCP 8443) avoids the app but needs Wi-Fi and the firewall opened
  **(untested)**.

## 4. Setup at the venue, step by step

### Step 1: phone
1. **Developer options:** Settings → About phone → tap **Build number** 7 times. Then Developer options → turn on
   **USB debugging**.
2. **Quiet the phone:** turn on **Airplane mode**, and leave Wi-Fi off too, because USB mode needs neither. Turn on
   **Do Not Disturb**, so no notification sound or call can grab the mic.
3. **Keep MicYou alive:** Settings → Apps → MicYou → Battery → **Unrestricted**, and Battery saver off. Set the
   display timeout to its longest, or turn on Developer options → **Stay awake** (screen stays on while charging;
   USB charges it).
4. **Lock MicYou in the app switcher.** Keep its foreground notification. MicYou's FAQ says Android otherwise
   suspends the mic and network when the screen locks.
5. **Plug in the data cable.** Accept **"Allow USB debugging?"** and tick "Always allow from this computer". If adb
   doesn't see the phone, switch the USB mode from "Charging only" to **File transfer**.

```bash
adb devices        # expect: <serial>   device      ("unauthorized" = accept the prompt on the phone)
```

### Step 2: USB-C microphone
Plug it in, then:

```bash
pactl list short sources | grep -i usb       # expect a line like: 61  alsa_input.usb-<Vendor>_<Product>-00.mono-fallback ...
journalctl -k -n 15 --no-pager | grep -i -E 'usb|audio'   # if nothing appears: the kernel saw no USB audio device
cat /proc/asound/cards                       # the mic should be card 1; today only card 0 (sof-hda-dsp, built in) exists
```

- If no `alsa_input.usb-…` source appears, the mic is not a USB audio device. Use the backup (section 9).
- **The profile.** A source name ending in `analog-stereo` or `mono-fallback` is right. A name containing `pro-input`
  means the card is on the **Pro Audio** profile: raw channels, often no usable volume. Switch it back:

```bash
pactl list short cards | grep usb                                    # the card name, alsa_card.usb-...
pactl list cards | sed -n '/Name: alsa_card.usb/,/Active Profile/p' | grep -E 'input:|Active Profile'
pactl set-card-profile alsa_card.usb-<...> input:mono-fallback       # or the input:analog-stereo one the list shows
```

- The profile changes the source name, so re-run `pactl list short sources` afterwards. This laptop's own card also
  offers `pro-audio`; it is on `HiFi`, which is right.
- **Use the same USB port all day.** A mic in a different port can get a different source name, and ears only
  follows the exact name it was started with.
- **Turn off the mic's own direct monitoring.** If it has a headphone jack and a "mix" or "monitor" knob, nobody
  should be listening on it.
- `pavucontrol` is not installed. Use the commands here, or KDE System Settings → Sound.

### Step 3: MicYou on the laptop

```bash
./MicYou-Linux-2.1.1.AppImage            # black window? add --software-rendering (or WEBKIT_DISABLE_DMABUF_RENDERER=1)
```

1. In MicYou desktop, choose **USB** mode. Leave the **output device on default/auto**.
   - MicYou only creates its PipeWire devices when no explicit output device is chosen.
   - With an explicit one, it would play the phone into the laptop speakers.
2. Keep any **monitoring** / "listen" option **off**.
3. On the phone, open MicYou, choose **USB**, and tap **Connect**.
   - MicYou runs `adb reverse tcp:<port> tcp:<port>` itself, so the audio goes over the cable.
   - This needs no Wi-Fi and is unaffected by the firewall. MicYou quotes "sub-10ms latency" for USB.

Check the devices it created:

```bash
pactl list short sources | grep -i micyou
# expect (as with MicYou's own pw-cli commands on this laptop):
#   ...  MicYouVirtualSink.monitor  PipeWire  float32le 2ch 48000Hz  IDLE
#   ...  MicYouVirtualMic           PipeWire  float32le 2ch 48000Hz  IDLE
```

- **Use `MicYouVirtualSink.monitor` as MIC1.** The launcher lists it and expects it, and its default-sink check
  (step 4) only works on a `.monitor` name.
- `MicYouVirtualMic` carries the same audio (a test tone measured identically through both) and also works, but then
  the launcher can't warn you about the default sink.
- Pick **one** of the two. Never give one to MIC1 and the other to MIC2: they are the same phone, the launcher can't
  tell, and every sentence would appear on both sides.

### Step 4: keep the laptop's sound out of the phone's mic

The dashboard's voice and the verdict music play to the **default sink**. If that is `MicYouVirtualSink`, its monitor
(MIC1) hears all of it, and mic1 transcribes it as Prop.

```bash
pactl list short sinks                 # pick the laptop speakers (or HDMI): an alsa_output.pci-... line
pactl set-default-sink <speakers>
pactl get-default-sink                 # must NOT be MicYouVirtualSink
```

- The launcher prints a `WARNING` (section 5) if the default sink is the phone's sink, but it still starts. Fix it
  there and then.
- **Never** pick `MicYouVirtualSink` as the laptop's output device in KDE's Sound settings either.
- Check again after any MicYou restart **(untested: whether MicYou changes the default sink)**.

### Step 5: name the mics

```bash
pactl list short sources | cut -f2                   # the current names
export MIC1=MicYouVirtualSink.monitor                # Prop, speaker A: the phone
export MIC2=alsa_input.usb-<Vendor>_<Product>-00.mono-fallback   # Opp, speaker B: paste the name from step 2
```

- The launcher checks both names when it starts, so no separate check is needed. Run it with no MIC1/MIC2 and it
  lists the usable sources (the laptop's own `alsa_output….monitor` sources are left out on purpose: they hear the
  speakers).
- For comparison, teammate A's USB mic was `alsa_input.usb-Samson_Technologies_Samson_Meteor_Mic-00.analog-stereo`
  (`ears/runstt.sh`).
- Export in the terminal where you'll start the ears, so a restart is up-arrow and Enter.

### Step 6: 5-second level check (and the bleed test)

Paste this once into the terminal. It defines `micmeter`, which meters one or two sources side by side for 5 s.
- It refuses names that don't exist, because a bare parec would silently meter the default mic.
- It starts its own two parec processes and stops exactly those, by their handles: no `pkill`.
- It only reads, so it can also run **while the ears are running**, to diagnose mid-demo. (The ears also log a peak
  level per mic every 15 s: section 6.)

```bash
micmeter() {
python3 - "$@" <<'PY'
import array, math, subprocess, sys
names = sys.argv[1:]
# parec never fails on a wrong name: PipeWire silently records the default mic instead. So check the names first.
known = {l.split("\t")[1] for l in subprocess.run(["pactl", "list", "short", "sources"],
                                                  capture_output=True, text=True).stdout.splitlines() if "\t" in l}
missing = [n for n in names if n not in known]
if not names or missing:
    sys.exit(f"No such source: {', '.join(missing) or '(none given)'}\nKnown: " + "  ".join(sorted(known)))
procs = [subprocess.Popen(["parec", "-d", n, "--format=s16le", "--rate=16000", "--channels=1", "--raw"],
                          stdout=subprocess.PIPE) for n in names]
db = lambda v: 20 * math.log10(max(v, 1) / 32768)
try:
    for tick in range(1, 11):                       # 10 x 0.5 s = 5 s
        cols = []
        for p in procs:
            raw = p.stdout.read(16000)
            if not raw:
                cols.append(f"{'NO AUDIO':<45}")
                continue
            a = array.array("h"); a.frombytes(raw[:len(raw) // 2 * 2])
            rms, peak = math.sqrt(sum(x * x for x in a) / len(a)), max(map(abs, a))
            bar = "#" * int(max(0, db(rms) + 60) / 3)
            cols.append(f"rms {db(rms):6.1f} peak {db(peak):6.1f} {'CLIP' if peak >= 32000 else '    '} {bar:<20}")
        print(f"{tick / 2:4.1f}s  " + " | ".join(cols), flush=True)
finally:
    for p in procs:                                 # only the parecs this started, by their own handles
        p.terminate()
        p.wait()
print("dBFS; columns: " + " | ".join(names))
PY
}
micmeter "$MIC1" "$MIC2"
```

Real output from this laptop: the internal mic (left, room noise, then a few words) and the empty headset jack (right).
The lines come in bunches of four because parec delivers 2 s at a time:

```
 3.5s  rms  -31.4 peak  -17.0      #########            | rms  -90.3 peak  -90.3
 4.0s  rms  -23.0 peak  -12.1      ############         | rms  -90.3 peak  -90.3
 4.5s  rms  -23.2 peak   -9.2      ############         | rms  -90.3 peak  -90.3
```

#### How to read it

| You see | Meaning | Do |
|---|---|---|
| `-90.3` steady | Digital silence: nothing connected, muted, or phone not streaming | Check the cable, MicYou connection and mute |
| Speaking: rms −30 to −18, peak −20 to −6 | Good | Leave it |
| `CLIP`, or peak above −3 | Too hot: Whisper mishears distorted words | Turn the gain down (below) |
| Speaking: rms below −40, or peak below −30 | Too quiet | Move the mic closer first, then add gain |
| Silent room: rms above −45 | Noisy: fan, venue PA or hum | Move the mic closer and lower the gain. On this laptop the **internal** mic sat at −31 to −36 in a quiet room at 100% |

Both mics should peak at a **similar level** for similar speech.

**The bleed test.** Do it once each way:
1. Prop talks at debate volume for 5 s while `micmeter "$MIC1" "$MIC2"` runs. MIC2's rms should be **at least 15 dB
   lower** than MIC1's.
2. Swap: Opp talks, and MIC1 should be at least 15 dB lower than MIC2.

Less than 10 dB apart: move the debaters apart, move the mics closer to their mouths, and lower both gains together.

#### Setting the gain

```bash
pactl set-source-mute "$MIC2" 0
pactl set-source-volume "$MIC2" 70%           # USB-C mic: hardware gain where the mic has it. Stay at or below 100%
pactl get-source-volume "$MIC2"
wpctl status | sed -n '/Sources:/,/Filters:/p' # the same, by PipeWire id: wpctl set-volume <id> 0.7
pactl set-sink-volume MicYouVirtualSink 100%  # phone: keep the sink at full level, so its monitor (MIC1) isn't scaled down
```

- **Phone side:** fix clipping **on the phone**, with the input gain in the MicYou app or the phone's mic gain.
  Clipping that happens on the phone can't be undone on the laptop.
- Turning `MicYouVirtualSink` down only scales the audio after it arrives, so it can lower a hot signal but can't add
  clean level.
- If words get chopped at their start, turn off MicYou's own VAD / "AI noise suppression" **(untested)**. Whisper has
  its own VAD.
- Leave the internal mic alone. It isn't used, and ears never falls back to it.

## 5. Run order

Use five terminals, all in `/home/dabest/claudesandbox/whack-merge` unless noted. **Start the ears before the debate,
do the sound check, then start the debate from the picker:** that clears the sound-check lines.

```bash
# T1: MicYou (step 3), already running.  T2: the meter (step 6) and the transcript tail (section 6).

# T3: the server (localhost only: /face/tts has no login)
HOST=127.0.0.1 EARS=external BOTS=real JUDGE=real SKIP_PIP=1 ./run.sh live
curl -s 127.0.0.1:8000/health | python3 -m json.tool | head -20    # "ok": true, ears "external"

# T4: the avatars (from the Mate Engine checkout; these PAYLOAD and DANCE_LIBRARY defaults exist on this laptop)
cd /home/dabest/claudesandbox/Mate-Engine-Linux-Port-main && tools/mate-bots.sh start
tools/mate-bots.sh status

# Browser: http://127.0.0.1:8000/?admin=1 full screen behind the avatars (no audio=1: the referee stays silent and
# the speakers carry only the verdict music. Add &audio=1 for a spoken referee, at the cost of more echo risk.)

# T5: the ears (MIC1/MIC2 exported in this terminal, step 5)
HF_HUB_OFFLINE=1 scripts/ears-two-mics.sh
#   stderr shows: bridge: sending to http://127.0.0.1:8000/publish as {'mic1': 'A', 'mic2': 'B'} / Loading Whisper model... /
#   Starting audio source mic1: MicYouVirtualSink.monitor / Starting audio source mic2: <MIC2> /
#   Listening to both sources. Press Ctrl+C to stop.
#   then, within 15 s, a peak-level line per mic (section 6)
```

- `SKIP_PIP=1` keeps `run.sh` from reaching for pip on venue Wi-Fi. Drop it if the requirements changed since the
  last install.
- The ears window is `WINDOW=6 HOP=1.5 HOLDBACK=2` with `MODEL=base.en`, the script's defaults: the cleanest
  settings measured on this CPU. Don't switch to `small.en` live; it's about 4x slower per window. `SERVER` (default
  `http://127.0.0.1:8000`) is the only other setting.

### When the launcher refuses to start

It checks before loading anything. Fix the cause and press up-arrow and Enter.

| T5 says | Exit | Do |
|---|---|---|
| `Set MIC1 and MIC2 to two different input sources (one microphone per debater):` and a list | 2 | Export both (step 5). The list is the usable sources |
| `MIC1 and MIC2 are both <name>: they must be two different sources, one per debater.` | 2 | Fix the export: one per debater |
| `MIC1=<name> is the monitor of the laptop's speakers: it hears what the laptop plays, not a debater.` | 2 | You picked an `alsa_output….monitor` (or `bluez_output`). Use the phone's `MicYouVirtualSink.monitor` |
| `MIC2=<name> is not a source right now (phone app not connected, mic unplugged, or a typo?). Sources:` and a list | 2 | Connect the phone app / plug in the mic, or copy the right name from the list |
| `WARNING: the default sink is MicYouVirtualSink, so MIC1 hears everything the laptop plays.` | runs on | Fix it now: `pactl set-default-sink <speakers>` (step 4) |
| `ears is already running (scripts/ears-two-mics.sh holds logs/ears.lock): stop that one first` | 1 | Another launcher is running (two would send every sentence twice). Find it with `fuser -v logs/ears.lock` and Ctrl+C it in its terminal |

## 6. The 60-second sound check

Each debater says, towards their own mic and at debate volume, a sentence with a number in it. For example: *"Prop
testing. Cars cause 29 per cent of city emissions."* Then they stop.

Watch four places:

```bash
# 1. The ears terminal (T5): every 15 s, one line per mic:
#   ears: mic1 peak level over the last 15 s: -12 dBFS
#   ears: mic2 peak level over the last 15 s: -14 dBFS

# 2. What Whisper heard, per mic (T2 or a sixth terminal):
tail -f "$(ls -t logs/ears-*.jsonl | head -1)"
#   {"type": "sentence", "source": "mic1", "start": ..., "end": ..., "text": "Prop testing."}

# 3. What reached the server, with the side it was given:
curl -s '127.0.0.1:8000/debug/history?n=40' | python3 -c 'import sys, json
for e in json.load(sys.stdin):
    if e["event"] in ("Utterance", "Pause", "Claim", "BotLine"):
        print(e["event"], e.get("speaker") or e.get("persona", ""), e.get("text", ""))'
```

4. **The dashboard:** Prop's line appears on Prop's side (A), and Opp's on Opp's side (B).

Pass if:
- each mic's level line, over a 15 s stretch where its debater spoke, peaks at roughly **−20 to −6 dBFS**, both at a
  similar level, with no `(quiet …)` hint and no `is clipping` line;
- each sentence appears **once**, from the **right mic**;
- the number survived ("29 per cent" or "29%");
- nothing appears from the other mic while one person talks.

A sentence that shows up from both mics means bleed: go back to step 6. Then click **New debate**, pick the Motion,
and the debate starts with A = Prop and B = Opp.

## 7. What latency to expect

These figures come from the code's timings and parec's measured 2 s bursts, not from a run with this hardware.

| Stage | Typical delay |
|---|---|
| Phone → laptop (MicYou over USB) | Under 10 ms (MicYou's figure) |
| End of a sentence → the line in `logs/ears-*.jsonl` | About **2.5–5 s**. parec hands audio over every 2 s, ears holds back the last 2 s of each 6 s window, re-transcribes every 1.5 s per mic, and the two mics take turns. **About 2 s more** if Whisper leaves out the full stop, because an unpunctuated tail only counts as a sentence after 2 s of quiet |
| Sentence → claim → bot bubble | A few seconds more (Claude calls). Each bot speaks **at most once every 10 s** (`BOTS_MIN_GAP_S`), so not every claim gets a bubble |
| End of a turn → `Pause` (when the referee may speak) | When the **other** debater's first sentence arrives (not a bleed copy), or after **6 s** with no new sentence from the speaker. After a long sentence it's the sentence's length + 1.2 s, up to 15 s. Those seconds count from when the last sentence **arrived**, so the referee speaks about 9 s or more after someone stops |

## 8. Debater instructions (read these out at the briefing)

1. **Speak towards your own mic,** at the same distance as in the sound check. Don't wave the phone around, and don't
   cover its bottom edge.
2. **One claim per sentence,** and finish each sentence: a clear stop, then about a 1 s breath. A full stop is what
   tells ears the sentence is done.
3. **Say numbers plainly with their unit:** "twenty-nine per cent", "eleven point five billion pounds", "in
   twenty-nineteen". Whisper writes them as digits. Avoid "like, roughly, sort of" around a figure.
4. **Never talk over each other, and don't heckle.** Even "no!" or "rubbish" into your own mic during the other turn
   ends their turn early and can let the referee cut in.
5. **At the end of your turn, stop dead and stay silent.** The other side starts when ready. The referee waits about 10 s
   of quiet (longer after a long sentence) before it speaks, so don't fill the silence.
6. Don't put a phone, a laptop or the speakers between you and your mic.

## 9. When things fail mid-demo

Restarting just the ears is safe and fast (about 3–5 s). In T5, press **Ctrl+C once**: ears writes its last
unfinished sentence to the transcript file and stops its parecs (each mic prints `Audio capture ended for micN`); the
bridge sends a `Pause` for whoever was mid-turn and both exit. Words spoken in the last moment before Ctrl+C reach only
`logs/ears-*.jsonl`, not the dashboard, so press Ctrl+C between turns. Then press up-arrow and Enter. The server, the avatars and the dashboard keep running, and
the debate carries on. Most mic drops no longer need a restart: ears recovers by itself (below).

**Never move ears' parec streams** in KDE's Sound settings (or pavucontrol): all parec streams share one
stream-restore entry, so moving one can move the others.

### What the ears terminal (T5) tells you

Lines starting with `!!` need you. `micN` is mic1 (Prop, phone) or mic2 (Opp, USB-C).

| Line | Meaning | Do |
|---|---|---|
| `ears: micN peak level over the last 15 s: -12 dBFS` | Normal, every 15 s per mic | Nothing. Watch that both mics stay in −20 to −6 dBFS while their debater speaks |
| `… peak level …: -38 dBFS (quiet: if someone spoke into it, move it closer or raise its gain)` | Peak below −30 dBFS. Fine if nobody spoke into it in those 15 s | If they did: move the mic closer, then raise the gain (step 6) |
| `… peak level …: no signal at all` | Exact zeros for those 15 s | See the digital-silence line below |
| `!! ears: micN (<source>) is clipping: turn its gain down (pactl set-source-volume <source> 70%, or the phone app's or the mic's own gain)` | Full-scale samples; at most every 10 s | USB-C mic: `pactl set-source-volume "$MIC2" 70%`. Phone: lower the MicYou app's or the phone's mic gain |
| `!! ears: micN (<source>) has sent pure digital silence for 15 s: is the phone app disconnected or muted, or the volume at 0? micN hears nothing until then` | The source is there but sends only zeros: typically the phone dropped off while MicYou desktop kept its devices. Said once | Reconnect the phone app (USB, Connect). Check the phone mic isn't muted and `pactl set-sink-volume MicYouVirtualSink 100%`. Wait for `ears: sound again from micN (<source>)` |
| `!! ears: audio capture for micN (<source>) ended (parec exit 1): micN is deaf; retrying in 0.5 s` | The source vanished mid-run (MicYou desktop closed, USB mic unplugged, PipeWire restarted). parec exited instead of moving to another device; parec's own `Stream error: Entity killed` may come just before | Bring the device back (restart MicYou and reconnect, or replug in the same port). No ears restart |
| `!! ears: <source> is not a current source (unplugged, phone app not connected, or renamed: see pactl list short sources): micN is deaf; retrying in <0.5\|1\|2> s` | The name isn't there. ears won't start parec on it and records nothing for that mic | As above. The third deaf line in a row (counting the `ended` line) ends with ` (saying so again only every 30 s)`; after that the line repeats only every 30 s. The retry delay grows 0.5 s, 1 s, then 2 s |
| `ears: capturing <source> again: hearing micN again` | Recovered (usually within 2 s of the device coming back) | Nothing. A new drop after this is announced at once |
| The deaf line keeps coming back every 30 s although the device is back | It came back under a **new name** (PipeWire can add a suffix such as `.2`; a USB mic in another port gets another name) | `pactl list short sources \| cut -f2`, `export MIC1=…` or `MIC2=…` with the new name, Ctrl+C, up-arrow, Enter |
| `!! ears: micN is falling behind: 3.2 s of audio was never transcribed. Use a smaller MODEL (base.en, tiny.en) or a longer WINDOW` | Whisper is too slow for this laptop; those words are lost | Make sure it's `MODEL=base.en` (the default); if it repeats, Ctrl+C and restart with `MODEL=tiny.en HF_HUB_OFFLINE=1 scripts/ears-two-mics.sh` (only once `tiny.en` has been downloaded, section 3; otherwise leave out `HF_HUB_OFFLINE=1`, which needs network). Never `small.en` live on this i5. Close other heavy programs |
| `bridge: send failed (try 1): …`, or `bridge: 502 from …` | The server is down or restarting. The bridge retries each event for up to 15 s | Get the server back (T3). Look for `bridge: http://127.0.0.1:8000/publish reachable again` |
| `bridge: gave up on an event after N tries` / `bridge: skipped an event N s old without sending it` | The server was gone longer than 15 s; that sentence or Pause was dropped on purpose (a late one would mislead) | Those sentences are only in `logs/ears-*.jsonl`. Carry on |
| `bridge: 401 from …` / `bridge: 422 from …` | Rejected, not retried (wrong password or URL, bad event) | Check `SERVER`; locally there is no password |
| `stdout closed; sentences go to the transcript file only` | bridge.py has exited: nothing reaches the server any more | Ctrl+C and restart the launcher. Harmless if it appears while you are stopping the ears with Ctrl+C |

### Symptoms

| Symptom | What's happening | Do |
|---|---|---|
| **Phone disconnects** (cable pulled, MicYou app killed on the phone) | MicYou desktop keeps its devices, so mic1 gets exact zeros. After 15 s: the `pure digital silence` line. Prop's sentences stop and the bridge sends Prop's Pause after 6 s | Reconnect in the phone app (USB, Connect). The audio resumes into the same source and ears says `sound again from mic1`; **no restart needed (untested with the real app)** |
| **MicYou desktop closed or crashed** | `MicYouVirtualSink` disappears. mic1 goes **deaf** (`ended (parec exit 1)`, then `not a current source`): it records nothing, never the laptop mic or the speakers (tested with null sinks, **untested with the real app**) | Restart the AppImage and reconnect the phone. Look for `hearing mic1 again`. Check the default sink again (step 4). If the deaf line keeps coming, the name changed: see the table above |
| **USB-C mic unplugged** | mic2 goes deaf the same way and picks the mic up again on replug (tested with null sinks, **untested with a real USB mic**) | Plug it back into the **same port** and look for `hearing mic2 again`. A different port can change the name: then re-export MIC2 and restart the ears |
| **One mic dead, can't fix in 30 s** | | Both debaters share the working mic, standing either side of it, and take strict turns. Sides are then wrong for one speaker, so say so. Or use the backup below |
| **Backup mic: earbuds in the 3.5 mm combo jack** | Appears as `alsa_input.pci-0000_00_1f.3-platform-skl_hda_dsp_generic.HiFi__Mic2__source` (it shows -90.3 with the jack empty today) | `export MIC2=<that name>` and restart the ears. **Plugging into the jack also moves laptop audio to the earbuds** (this card's Headphones vs Speaker profiles), so send the music to HDMI: KDE Sound settings, or `pactl set-default-sink <HDMI sink>` |
| **Words on the wrong side** | MIC1 and MIC2 are swapped, or the debate was started without the picker | Quickest: **swap the physical mics** (hand the phone to the other debater). Or swap the two exports and restart the ears. Always start the debate from the picker: without sides, whoever speaks first becomes Prop |
| **Everything appears twice, once per side** | Bleed: debaters too close or gain too high. Or MIC1 and MIC2 are two names for the same phone (`MicYouVirtualMic` and `MicYouVirtualSink.monitor`), which the launcher can't catch | Separate the debaters, lower both gains, and check `echo $MIC1 $MIC2` |
| **Gibberish or missing words** | Clipping, too quiet, or falling behind | Check T5 for `is clipping`, `(quiet …)` or `falling behind`; `micmeter` also runs alongside the ears. Fix the gain (step 6) |
| **The laptop's sound transcribed as Prop** | The default sink is `MicYouVirtualSink` | `pactl set-default-sink <speakers>` (step 4) |
| **Referee's voice transcribed as a debater** (only with `audio=1`) | Speakers too loud or pointed at a mic | Lower the volume and turn the speakers away. In admin, **M** mutes |
| **Feedback howl** | Something plays a mic to the speakers: MicYou monitoring, the USB mic's direct monitor, or `MicYouVirtualSink` set as output | Turn that off. `pactl get-default-sink` must be the speakers or HDMI |
| **Music after the verdict reaching the mics** | Expected: the winner's dance plays music | Ctrl+C the ears right after clicking **Final screen**, or start the server with `JUDGE_MUSIC=none` |
| **Nothing works** | | Fall back as in `DEMO.md`: admin bar → **Replay recorded debate** |

## 10. Alternatives to MicYou

- **MicYou over Wi-Fi** works, but it's the weaker choice at a venue: shared Wi-Fi often isolates clients (AP
  isolation), and it adds jitter. This laptop's **firewalld** (`public` zone: ssh and dhcpv6-client only) also blocks
  it until you open the ports for this boot:
  ```bash
  sudo firewall-cmd --add-port=8554/tcp --add-port=8555/udp
  ```
  Use a phone hotspot rather than venue Wi-Fi.
- **AudioRelay** (Android) has a Linux "phone as a microphone" mode.
  - It plays into a PulseAudio null sink you create, `audiorelay-virtual-mic-sink`, with a remapped source.
  - It connects over Wi-Fi or **USB tethering**. Tethering adds a network interface, and the laptop may route traffic
    through the phone.
  - It's a fallback only **(untested)**: <https://audiorelay.net/docs/linux/use-your-phone-as-a-mic-for-a-linux-pc>.
- **scrcpy** 2.x can forward the phone **mic** over adb (`--audio-source=mic`, Android 11+). It plays the mic to a
  laptop output, so you'd have to route it into a null sink and capture that sink's monitor. That's too fiddly to set
  up on the day **(untested)**.
- **The phone's own recorder app is not an option.** Nothing reaches the laptop live.
- **The phone's browser, no app at all:** the server's own `/mic` page, with `PHONE_MICS=A,B`. See
  [section 12](#12-phone-as-a-microphone-without-an-app).

## 11. Printable pre-demo checklist

**Night before / with internet**
- [ ] `sudo pacman -S android-tools` done; `adb devices` lists the phone as `device`
- [ ] MicYou AppImage on the laptop; MicYou APK on the phone
- [ ] USB-C mic shows as `alsa_input.usb-…` on the `analog-stereo` / `mono-fallback` profile (not `pro-input`)
- [ ] One full rehearsal through both mics **(nobody has tested the real phone and USB-C mic yet)**: both level lines
      look right; restart the phone app once → `sound again from mic1` (or `hearing mic1 again`); unplug and replug
      the USB mic once → `hearing mic2 again` (or the `not a current source` line if it came back under a new name);
      sentences show on the dashboard as A and B

**T−30 min**
- [ ] Phone: Airplane mode, Do Not Disturb, Battery saver off, MicYou battery Unrestricted, Stay awake, MicYou locked in the app switcher
- [ ] Data cable in; "Allow USB debugging" accepted
- [ ] Laptop on its charger, notifications off, no sleep; headphones **out** of the jack (this laptop is on the Headphones profile while they're in)
- [ ] MicYou desktop in USB mode, output device default/auto, monitoring off; phone connected
- [ ] `pactl list short sources` shows `MicYouVirtualSink.monitor` and the USB mic
- [ ] `pactl get-default-sink` is the speakers or HDMI, **not** `MicYouVirtualSink`
- [ ] `export MIC1=MicYouVirtualSink.monitor MIC2=<usb name>`
- [ ] `micmeter "$MIC1" "$MIC2"`: speech peaks −20 to −6 dBFS, no `CLIP`; silence below −45
- [ ] Bleed test both ways: the far mic is at least 15 dB lower
- [ ] Debaters at least 2 m apart, mics 10–20 cm from their mouths and pointed apart, speakers aimed at the audience

**T−15 min**
- [ ] Server up (`/health` ok), avatars up (`mate-bots.sh status`), dashboard `/?admin=1` full screen
- [ ] Ears running (`HF_HUB_OFFLINE=1 scripts/ears-two-mics.sh`): no refusal, no `WARNING`, "Listening to both sources."
- [ ] Both `peak level` lines appear in T5; no `!!` lines
- [ ] Sound check: each debater's test sentence appears once, on the right side, with its number intact
- [ ] **New debate** → Motion picked → A = Prop (phone), B = Opp (USB-C)
- [ ] Debaters briefed (section 8); the operator knows Ctrl+C → up-arrow → Enter to restart the ears, and the `!!` lines (section 9)

**After**
- [ ] Ctrl+C the ears at **Final screen** (or `JUDGE_MUSIC=none`)
- [ ] `tools/mate-bots.sh stop`

## 12. Phone as a microphone without an app

The server can take a phone's microphone straight from the phone's **browser**: no MicYou, nothing installed on the
phone. It's an alternative to MicYou for either debater, or for both (two phones).

```
phone browser /mic?ch=A --WebSocket, 16 kHz s16le--> server /mic/ws --> pacat --> null sink "whack_phone_A"
                                                                                  |
                                       ears (MIC1=whack_phone_A.monitor) <--------+   (exactly like any other source)
```

- The page (`/mic?ch=A` for Prop, `/mic?ch=B` for Opp) shows the side in big letters, a **Start/Stop** button, a live
  level meter, the connection state, and a clear error when something's wrong. While the mic is live it keeps the
  screen on (Wake Lock), where the browser supports it.
- It captures with echo cancellation and noise suppression on. The phone's auto gain is on too; add `&agc=0` to turn
  it off if the level pumps up in pauses. The page downsamples to 16 kHz mono in an AudioWorklet and sends 80 ms frames.
- The server writes each phone's audio into its own null sink. The sinks exist only when the server is started with
  `PHONE_MICS` (e.g. `PHONE_MICS=A,B`, or `PHONE_MICS=A` for one phone plus the USB-C mic). They are created at
  start-up and unloaded at shutdown. Without a phone connected, their monitors are pure silence.
- **One phone per side.** A second phone on the same side is refused ("another phone is already microphone A"), and
  so is a side that isn't in `PHONE_MICS`. A dropped connection reconnects on its own with backoff (0.5 s up to 10 s).
- The dashboard gets a status dot per phone: `phone_A` is amber while it waits for a phone, green while one streams,
  and red if the sink couldn't be created.
- **Browsers only give a page the microphone in a secure context:** `https://…`, or `http://localhost`. So use USB
  with `adb reverse` (recommended), or HTTPS over Wi-Fi.

### Over USB (recommended): adb reverse

Needs `adb` on the laptop (`sudo pacman -S android-tools`) and USB debugging on the phone (step 1 in section 4).

```bash
# T3: the server, still localhost only
HOST=127.0.0.1 PHONE_MICS=A EARS=external BOTS=real JUDGE=real SKIP_PIP=1 ./run.sh live
#   log: phone mic A: sink whack_phone_A (module <id>); ears source whack_phone_A.monitor

adb reverse tcp:8000 tcp:8000          # the phone's localhost:8000 is now the laptop's; redo it after a replug
```

On the phone, open **`http://localhost:8000/mic?ch=A`** in Chrome, tap **Start**, and allow the microphone. The state
line says "Live: microphone A (Proposition) is on the laptop". Then start the ears with the sink's monitor:

```bash
export MIC1=whack_phone_A.monitor MIC2=<usb name>      # or MIC2=whack_phone_B.monitor with a second phone
HF_HUB_OFFLINE=1 scripts/ears-two-mics.sh
```

The launcher accepts `whack_phone_A.monitor` like any other source, and `micmeter "$MIC1" "$MIC2"` (step 6) works on
it too. Start the server **before** the ears: the sink only exists while the server runs, so the launcher refuses
the name otherwise ("is not a source right now").

### Over Wi-Fi: HTTPS and a token

Plain `http://<laptop IP>:8000` is not a secure context, so the page will say "Not a secure context" and the mic stays
off. Serve HTTPS with a self-signed certificate:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 30 -keyout cache/key.pem -out cache/cert.pem \
  -subj "/CN=whack-laptop" -addext "subjectAltName=IP:<laptop IP>,DNS:localhost"
export MIC_TOKEN=$(openssl rand -hex 8)        # keep it out of shell history on a shared screen
HOST=0.0.0.0 PHONE_MICS=A,B SSL_CERTFILE=cache/cert.pem SSL_KEYFILE=cache/key.pem \
  EARS=external BOTS=real JUDGE=real ./run.sh live
```

On the phone, open `https://<laptop IP>:8000/mic?ch=A&token=<MIC_TOKEN>`. Accept the certificate warning once
(Advanced, Proceed). The dashboard is then also `https://…`.

- **A LAN bind exposes more than the mic.** `HOST=0.0.0.0` serves the whole server to the network: `/face/tts` (no
  login), `/publish`, the dashboard, and the mic socket. Anyone on the network could speak as a debater, hence
  `MIC_TOKEN`: with it set, a connection without the right `?token=` is refused. The server logs a warning when it
  binds to the LAN with `PHONE_MICS` and no `MIC_TOKEN`. Prefer USB.
- Venue Wi-Fi often isolates clients, and firewalld blocks port 8000 until you open it for this boot:
  `sudo firewall-cmd --add-port=8000/tcp`. A phone hotspot is more reliable.

### On the phone

- Keep the page in the foreground with the screen on. Android may stop a background tab's mic, so don't switch apps.
  Set Battery saver off, Do Not Disturb on, and Airplane mode on for USB (Wi-Fi back on for the Wi-Fi route).
- Don't use the same phone for MicYou and the page at once: the mic is busy, and the page says so.
- The level meter is what the phone captures, before the network. Aim for speech peaks around −20 to −6 dBFS. Set
  the gain by distance; `CLIP` means move the phone back. Then check with `micmeter` on the laptop, which hears what
  ears hears.

### When it fails

| The phone page says | Do |
|---|---|
| `Not a secure context: …` | You opened `http://<IP>`. Use `adb reverse` and `http://localhost:8000/…`, or the HTTPS route |
| `Microphone permission denied. …` | Allow the microphone in the site settings (the icon left of the address), then Start |
| `The microphone is busy: …` | Close MicYou, a call, or a recorder app, then Start |
| `This microphone is not switched on at the server: …` | Restart the server with `PHONE_MICS=A,B` (or the side you need) |
| `The server wants a token: …` | Add `&token=<MIC_TOKEN>` to the address |
| `Another phone is already this microphone. …` | Stop the other phone (or close its tab), then Start |
| `Connection lost; retrying in …`, then `Cannot reach the server …` | The server is down, or the cable was replugged: run `adb reverse tcp:8000 tcp:8000` again. The page reconnects by itself |

While the phone is disconnected, ears hears **silence** on that sink, never another device: after 15 s, the
`pure digital silence` line from section 9. It says `sound again from micN` once the phone is back. If the sink itself is gone (the server stopped), ears goes **deaf** as in section 9. Restart the
server, then the ears.

**What was verified:** on this laptop, with no phone, a WAV was streamed through `/mic/ws` by a small Python client
over HTTP and over HTTPS. `parec` on `whack_phone_A.monitor` heard it: RMS about 2540, against exactly 0 with no
phone. A second connection was refused, and the sinks were unloaded at shutdown. The page itself is tested against a
fake browser in `tests/js/`. **A real phone has not been tested: rehearse it.**

## Sources
- MicYou project and releases: <https://github.com/MicYou-Dev/MicYou>, <https://github.com/MicYou-Dev/MicYou/releases> (v2.1.1, 11 Oct 2026)
- MicYou Quick Start (USB/ADB steps, ports 8554/tcp and 8555/udp, PipeWire on Linux): <https://micyou.top/en/docs/quick-start>
- MicYou FAQ (battery and background, adb "unauthorized", charging-only mode, latency, gain): <https://micyou.top/en/docs/faq>
- MicYou iOS install (sideloading, iOS 18+): <https://micyou.top/en/docs/how-to-install-in-ios>
- MicYou source: `tauri-app/crates/micyou-core/src/platform/pipewire.rs` (`MicYouVirtualSink`, `MicYouVirtualMic`,
  the default-source save and restore), `.../platform/adb.rs` (`adb reverse`), `.../server/output.rs` (devices are
  only created with no explicit output device)
- AudioRelay: <https://audiorelay.net/docs/linux/use-your-phone-as-a-mic-for-a-linux-pc>, <https://audiorelay.net/docs/usb>
- scrcpy audio: <https://github.com/Genymobile/scrcpy/blob/master/doc/audio.md>
- WirePlumber settings (`target.object`): <https://pipewire.pages.freedesktop.org/wireplumber/daemon/configuration/settings.html>
- This repo: `scripts/ears-two-mics.sh` (name checks, `logs/ears.lock`), `ears/ears.py` (`Capture`,
  `listed_sources`, `LevelWatch`, the falling-behind line), `ears/bridge.py` (retries, bleed rule), `app/brain/run.py`
  (BleedGuard, EchoGuard), `docs/SIDE-BOTS.md`, `docs/DEMO.md`, and the Mate Engine checkout's `tools/mate-bots.sh`
