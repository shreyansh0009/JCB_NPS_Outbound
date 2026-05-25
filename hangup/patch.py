"""
Apply auto-hangup patches to a streaming_pipeline.py.

Usage:
    python hangup/patch.py core/streaming_pipeline.py

Applies 6 targeted changes that fix two bugs:
  Bug 1 — _drain() silently removes the None sentinel:
    When end_call=True, the None that signals _send_audiosocket to stop can be
    wiped by barge-in (energy or transcript) before it's consumed, leaving the
    call alive indefinitely.
  Bug 2 — PSTN call stays open during post-processing:
    Without closing the writer immediately after the final audio, Asterisk holds
    the PSTN leg open through recording upload + transcript saving (15-20s).

Safe to run multiple times — patches already applied are silently skipped.
"""
import sys
import re


PATCHES = [
    # ── 1. Add end_call_requested flag to _TurnState ────────────────────────
    {
        "id": "end_call_requested_field",
        "description": "Add end_call_requested field to _TurnState",
        "sentinel": "end_call_requested: bool = False",
        "find": "    call_ended: bool = False\n    barge_in: asyncio.Event = field(default_factory=asyncio.Event)",
        "replace": (
            "    call_ended: bool = False\n"
            "    end_call_requested: bool = False  # set when agent signals end_call; blocks new turns\n"
            "    barge_in: asyncio.Event = field(default_factory=asyncio.Event)"
        ),
    },
    # ── 2. Block energy barge-in after end_call is signaled ─────────────────
    {
        "id": "barge_in_energy_guard",
        "description": "Guard energy barge-in against draining the None sentinel",
        "sentinel": "if state.end_call_requested:\n                return\n\n            logger.info(\n                f\"[{call_sid}] BARGE-IN detected",
        "find": (
            "        if state._hot_frames >= _BARGE_CONFIRM_FRAMES:\n"
            "            # ── BARGE-IN CONFIRMED ──\n"
            "            logger.info(\n"
            "                f\"[{call_sid}] BARGE-IN detected"
        ),
        "replace": (
            "        if state._hot_frames >= _BARGE_CONFIRM_FRAMES:\n"
            "            # ── BARGE-IN CONFIRMED ──\n"
            "            # After end_call is signaled, the None sentinel is in the audio queue.\n"
            "            # Allowing barge-in here would drain it away and stall call teardown.\n"
            "            if state.end_call_requested:\n"
            "                return\n"
            "\n"
            "            logger.info(\n"
            "                f\"[{call_sid}] BARGE-IN detected"
        ),
    },
    # ── 3. Block transcript-based new turns after end_call ───────────────────
    {
        "id": "on_transcript_guard",
        "description": "Guard _on_transcript against starting new turns after end_call",
        "sentinel": "if state.call_ended or state.end_call_requested:",
        "find": "        if state.call_ended:\n            return\n        active_orchestrator = orchestrator or self.orchestrator",
        "replace": (
            "        if state.call_ended or state.end_call_requested:\n"
            "            return\n"
            "        active_orchestrator = orchestrator or self.orchestrator"
        ),
    },
    # ── 4. Set end_call_requested before queuing None in _handle_turn ────────
    {
        "id": "handle_turn_set_flag",
        "description": "Set end_call_requested flag before queuing None sentinel",
        "sentinel": "state.end_call_requested = True\n                await audio_out.put(None)",
        "find": (
            "            if final_response and final_response.end_call:\n"
            "                # NOTE: Do NOT set state.call_ended here — it would cause remaining\n"
            "                # TTS audio frames to be skipped in _send_audiosocket (line 763).\n"
            "                # Instead, just send the None terminator. The _send_audiosocket will\n"
            "                # exit when it sees None, which triggers asyncio.wait() in _run_call_inner\n"
            "                # to return and properly set state.call_ended = True during cleanup (line 439).\n"
            "                # This ensures the closing statement TTS is fully sent before Deepgram closes.\n"
            "                await audio_out.put(None)"
        ),
        "replace": (
            "            if final_response and final_response.end_call:\n"
            "                # Set end_call_requested BEFORE queuing the None so that any barge-in\n"
            "                # or noise transcript that arrives while the goodbye TTS is draining\n"
            "                # cannot call _drain(audio_out) and silently remove the None sentinel.\n"
            "                # Do NOT set state.call_ended here — that would cause remaining TTS\n"
            "                # frames to be skipped in _send_audiosocket.\n"
            "                state.end_call_requested = True\n"
            "                await audio_out.put(None)"
        ),
    },
    # ── 5. Don't schedule no-response timer after end_call ───────────────────
    {
        "id": "no_response_timer_guard",
        "description": "Skip no-response timer scheduling when end_call is requested",
        "sentinel": "if turn_spoke and not state.end_call_requested:",
        "find": (
            "                    if turn_spoke:\n"
            "                        self._schedule_no_response_timer(audio_out, session, call_sid, state)"
        ),
        "replace": (
            "                    if turn_spoke and not state.end_call_requested:\n"
            "                        self._schedule_no_response_timer(audio_out, session, call_sid, state)"
        ),
    },
    # ── 6. Send HANGUP frame + close writer in _send_audiosocket ─────────────
    {
        "id": "send_hangup_and_close",
        "description": "Send HANGUP frame and close writer after final audio drains",
        "sentinel": "from hangup import send_hangup_frame",
        "find": (
            "            # Final drain: flush any remaining bytes in the transport buffer\n"
            "            try:\n"
            "                await writer.drain()\n"
            "            except Exception:\n"
            "                pass\n"
            "        except asyncio.CancelledError:\n"
            "            raise\n"
            "        except Exception:\n"
            "            logger.exception(f\"[{call_sid}] _send_audiosocket error\")"
        ),
        "replace": (
            "            # Final drain: flush any remaining bytes in the transport buffer\n"
            "            try:\n"
            "                await writer.drain()\n"
            "            except Exception:\n"
            "                pass\n"
            "            # Signal Asterisk to hang up and close the TCP connection immediately,\n"
            "            # before post-processing begins.  Two steps are required:\n"
            "            # 1. HANGUP frame (0xFF) — tells Asterisk's AudioSocket app to exit.\n"
            "            # 2. writer.close() — closes the TCP socket so Asterisk drops the PSTN leg\n"
            "            #    even if it doesn't react to the frame alone (version-dependent).\n"
            "            # Without both, the PSTN call can stay alive through all of recording\n"
            "            # upload + transcript saving (up to 15-20s of dead silence for the caller).\n"
            "            try:\n"
            "                from hangup import send_hangup_frame\n"
            "                await send_hangup_frame(writer)\n"
            "            except Exception:\n"
            "                pass\n"
            "            try:\n"
            "                writer.close()\n"
            "            except Exception:\n"
            "                pass\n"
            "        except asyncio.CancelledError:\n"
            "            raise\n"
            "        except Exception:\n"
            "            logger.exception(f\"[{call_sid}] _send_audiosocket error\")"
        ),
    },
]


def apply_patches(filepath: str) -> None:
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    changed = False
    for patch in PATCHES:
        pid = patch["id"]
        if patch["sentinel"] in content:
            print(f"  [skip]  {pid} — already applied")
            continue
        if patch["find"] not in content:
            print(f"  [FAIL]  {pid} — original text not found; check streaming_pipeline.py version")
            continue
        content = content.replace(patch["find"], patch["replace"], 1)
        print(f"  [OK]    {pid} — {patch['description']}")
        changed = True

    if changed:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"\nPatched: {filepath}")
    else:
        print(f"\nNo changes written (all patches already applied or not matched).")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python hangup/patch.py path/to/streaming_pipeline.py")
        sys.exit(1)
    apply_patches(sys.argv[1])
