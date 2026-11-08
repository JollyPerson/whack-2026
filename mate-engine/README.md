# Mate Engine changes for the side bots and the referee avatar

The two side-bot avatars (For / Against) and the referee avatar are Mate Engine with our changes, kept here as a patch
series. There is no separate fork repository. The patches apply on top of the unofficial Linux port
[Marksonthegamer/Mate-Engine-Linux-Port](https://github.com/Marksonthegamer/Mate-Engine-Linux-Port) at commit
`ee4072a` ("Added override_redirect").

| Patch | What it adds |
|---|---|
| 0001 | Speech bridge (long-polls the debate server, speech bubble, mouth), dance-player fixes, command-line build (`build-referee.sh`) and dance export |
| 0002, 0005 | Bubble beside the head on a chosen side, sized in screen pixels; the mouth moves while text-only lines type |
| 0003, 0008, 0011 | `tools/mock_mate_server.py`: a mock debate server with the side-bot channels, verdict dances and an older-server mode |
| 0004, 0006, 0007, 0010 | `tools/mate-bots.sh`: launches the two side-bot avatars (own config, channel, window, avatar) and installs the verdict dances |
| 0009 | Dance music on/off and automatic stop, for the verdict dances |
| 0012 | Credits and a note about what this fork is |

The protocol, every setting and the credits are in `Assets/MATE ENGINE - Referee/README.md` once the patches are
applied. Running the whole system is covered in [`docs/SIDE-BOTS.md`](../docs/SIDE-BOTS.md).

## Apply and build
```bash
git clone https://github.com/Marksonthegamer/Mate-Engine-Linux-Port.git
cd Mate-Engine-Linux-Port
git checkout -b side-bots ee4072a
git am /path/to/whack-2026/mate-engine/patches/*.patch   # trailing-whitespace warnings come from Unity .meta files
./build-referee.sh ~/mate-builds/bots                    # Unity 6000.2.6f2; the first project import takes ~12 min
```
- **Unity:** the build needs Unity 6000.2.6f2 with Linux build support. On Arch, Unity also needs `libxml2-legacy`
  (it links `libxml2.so.2`).
- **File dialogs:** `libStandaloneFileBrowser.so` is gitignored upstream. Set `PREBUILT_PAYLOAD=<a released
  MateEngineX>/Payload` so the build script copies it in; without it the build works, but file dialogs don't.

## Run the two avatars
```bash
PAYLOAD=~/mate-builds/bots/Payload DANCE_LIBRARY=<CustomDances folder> SERVER=http://127.0.0.1:8000 \
  tools/mate-bots.sh start          # also: stop | status | place
```
The verdict dances are not included (see Credits). Without `DANCE_LIBRARY`, everything works except that the
`dance` commands find no dance to play.

## Licence and credits
- These patches modify Mate Engine, so they are a derivative work under the **MateEngine Pro License v2.0**: a copy
  is in `LICENSE`.
  - Use is non-commercial and free of charge, and attribution must be kept.
  - The licence also requires the modified source to be **publicly available**. This repository is private for now:
    make it (or at least this folder) public before distributing a build.
- **[Mate Engine](https://github.com/shinyflvre/Mate-Engine)** is by shinyflvre (Shiny / Johnson Jason) and
  contributors. The **[Linux port](https://github.com/Marksonthegamer/Mate-Engine-Linux-Port)** is by
  Marksonthegamer. Neither project is affiliated with or endorses whack-2026.
- The bubble mirrors `LLMUnitySamples.Bubble` from [LLMUnity](https://github.com/undreamai/LLMUnity) by Undream AI
  (MIT).
- The verdict dances ("MMD-Conqueror", "MMD-Lost One_s Weeping", "MMD-皆大欢喜-卜逻desu") come from the Mate Engine
  community's CustomDances collection. They belong to their MMD motion and music creators and are not redistributed.
