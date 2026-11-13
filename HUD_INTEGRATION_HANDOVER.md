# 🌌 JARVIS Mark LVI — HUD Integration Handover Specification

> **Copy & paste this document into the chat where you are designing the HUD.**

---

## ⚠️ STRICT ARCHITECTURAL BOUNDARY (DO NOT TOUCH)

When integrating the HUD into `S:\Projects\J.A.R.V.I.S\ui.py`, you must obey these strict safety constraints:

1. **DO NOT TOUCH the bottom UI:**
   - The `LISTENING` / `THINKING` / `SPEAKING` / `MUTED` status indicator and its exact colors must remain **completely unchanged**.
2. **DO NOT TOUCH the audio waveform:**
   - The live speaking audio waveform bar visualizer situated below the circle must remain **completely unchanged**.
3. **DO NOT TOUCH any other files or subsystems:**
   - Do not alter `actions/*` (all 21 computer actions).
   - Do not alter `plugins/*` (all 11 verified plugins: WhatsApp, Gmail, Calendar, Telegram, Aviation Weather, Document Review, Pushup/Squat Counter, etc.).
   - Do not alter `main.py`, `core/*`, voice pipeline, system metrics, or configuration.
4. **ONLY MODIFY:**
   - **`HudCanvas` in `S:\Projects\J.A.R.V.I.S\ui.py`** — Replace *only* the internal rotating circular avatar/HUD paint logic with the 3D Holographic JARVIS Orb described below.

---

## 🎯 EXACT TARGET REFERENCE: Avengers: Age of Ultron Hologram Orb

The design must faithfully match the live-action movie footage from Avengers: Age of Ultron (`WhatsApp_Video_2026-11-12_at_20.50.09.mp4` / `scratch/wa_orb_zoom.jpg`).

### 1. Central Core ("The Eye") — `r = 0.08 .. 0.14`
- **Dark Hollow Pupil:** The exact center is dark/hollow.
- **NO Center Text:** The inner `"JARVIS"` text label was explicitly **rejected** by the user and must **not** be present.
- **NO Blinding Sun Dot:** Do not draw a solid white circle or bright sun blob in the center.
- **Blazing Golden Torus:** 8–10 concentric, tightly wound golden-yellow iris filament loops enclosing the hollow pupil.

### 2. Inner Orbital Accents — `r = 0.20 .. 0.38`
- **Swept Orbital Crescent / Comma Arc:** A distinct curved crescent ribbon tilted obliquely across the core from ~10:30 to 4:30.
- **Crossing Laser Chords:** 5–7 delicate straight tangent beam rays cutting lightly across the sphere and grazing past the core.
- **Inner Slotted Tick Track:** Circular rail at `r = 0.22` with fine radial tick marks rotating counter-clockwise.
- **Mid Gear Rail:** Dual circular rail at `r = 0.38` with radial gear teeth rotating clockwise.

### 3. Dense 3D Spherical Crust — `r = 0.46 .. 0.78`
- **Volumetric Energy Fill:** The sphere between the core and outer shroud is **densely packed** with glowing light (it must never look like an empty void).
- **200+ Tangled Golden Filaments:** Sinuous 3D surface ringlets and organic radial root dendrites wrapping the spherical volume.
- **600+ Holographic Bokeh Particles:** Sparkling golden ember motes distributed in 3D spherical space, twinkling with subtle phase offsets.
- **Voice-Reactive Synaptic Pulse:** During `THINKING` and `SPEAKING`, an outward-radiating golden luminescence wave travels from the core through the filaments.

### 4. Concentric Counter-Rotating Outer Shroud — `r = 0.76 .. 0.98`
- **Primary Stepped Slotted C-Shroud Bracket (`r = 0.76 .. 0.92`):**
  - A prominent curved ladder bracket spanning ~`240°–260°`, leaving an open ~`100°–120°` notch.
  - **90° Inward Radial Elbows:** Both ends of the notch make crisp 90° inward radial turns.
  - **Radial Ladder Rungs & Fins:** Closely spaced radial rungs connecting parallel circular rails, with occasional protruding mechanical anchor pins/fins.
  - **Continuous Wire Guides:** Faint thin circular wire rails continue seamlessly across the open notch to complete the ring.
  - **Motion:** Rotates slowly and steadily **clockwise** (~`+0.12 rad/s`).
- **Secondary Counter-Bracket:** Upper-right bracket rotating counter-clockwise (~`-0.09 rad/s`).
- **Outermost Sweeping Ghost Ellipses (`r = 0.94 .. 0.98`):** 2–3 very faint, thin tilted circular wire guides in dim copper-amber.

### 5. Color Palette & Lighting
- **Pure Black Canvas:** `#000000` (or `#00060a`) with additive blending (`QPainter.CompositionMode_Plus`).
- **Deep Rust / Burnt Copper:** `#a03404` to `#dc5810` for the dense inner web filaments.
- **Luminous Golden-Yellow:** `#ffc434` to `#ffe66e` for the core torus, tick tracks, and stepped C-shroud.
- **Hot White-Gold Flares:** `#fff8d0` on high-intensity intersections and laser chords.
- **Optical Hologram Bloom:** 2-stage bloom (tight Gaussian blur ~3.8px + wide atmospheric amber glow ~12.0px).

### 6. Pacing & Orientation
- **Weightless Cinematic Motion:** Rotates gently at ~`0.10–0.14 rad/s` (~25–35 seconds per full turn). It must **never** spin fast like a loading indicator.
- **Frontal 3D Perspective:** Kept primarily facing the camera with a subtle `±12°` gyroscopic pitch/yaw precession. It must **never** flip edge-on into a flat line.

---

## 📁 Reference Files in Scratch Directory
If you need to inspect the working prototype and rendered reference frames:
- **Renderer Script:** [preview_3d_hud.py](file:///C:/Users/ASUS/.gemini/antigravity/brain/7841af28-6fe0-4cfe-b3c0-04e89dbfd15d/scratch/preview_3d_hud.py)
- **Rendered 12s MP4:** [jarvis_3d_hud_preview.mp4](file:///C:/Users/ASUS/.gemini/antigravity/brain/7841af28-6fe0-4cfe-b3c0-04e89dbfd15d/scratch/jarvis_3d_hud_preview.mp4)
- **Frame Overview Sheet:** [preview_sheet.jpg](file:///C:/Users/ASUS/.gemini/antigravity/brain/7841af28-6fe0-4cfe-b3c0-04e89dbfd15d/scratch/preview_sheet.jpg)
- **Movie Zoom Frames:** [wa_orb_zoom.jpg](file:///C:/Users/ASUS/.gemini/antigravity/brain/7841af28-6fe0-4cfe-b3c0-04e89dbfd15d/scratch/wa_orb_zoom.jpg)
