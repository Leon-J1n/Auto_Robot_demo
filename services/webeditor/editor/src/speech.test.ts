// @vitest-environment happy-dom
import { readFileSync } from "node:fs";
import { URL as NodeURL } from "node:url";
import { transpile } from "typescript";
import { afterEach, expect, it, vi } from "vitest";
import { enterSendsAgentMessage } from "./logic";
import { CAPTURE_WORKLET, SpeechInput } from "./speech";
import { Pcm16Resampler } from "./speech_audio";

class Socket {
  static OPEN = 1;
  static instances: Socket[] = [];
  readyState = 1;
  bufferedAmount = 0;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  send = vi.fn();
  close = vi.fn();
  constructor(public url: string) { Socket.instances.push(this); }
  message(payload: object) { this.onmessage?.({ data: JSON.stringify(payload) }); }
}
class Worklet {
  static instances: Worklet[] = [];
  connect = vi.fn();
  disconnect = vi.fn();
  port = { onmessage: null as ((event: { data: unknown }) => void) | null,
    postMessage: vi.fn((message: string) => {
      if (message === "flush") queueMicrotask(() => this.port.onmessage?.({ data: "flushed" }));
    }) };
  constructor() { Worklet.instances.push(this); }
}
class Context {
  sampleRate = 48000;
  resume = vi.fn(async () => undefined);
  close = vi.fn(async () => undefined);
  audioWorklet = { addModule: vi.fn(async () => undefined) };
  createMediaStreamSource = vi.fn(() => ({ connect: vi.fn(), disconnect: vi.fn() }));
}
function setup() {
  Socket.instances = []; Worklet.instances = [];
  vi.stubGlobal("WebSocket", Socket);
  vi.stubGlobal("AudioContext", Context);
  vi.stubGlobal("AudioWorkletNode", Worklet);
  Object.defineProperty(window, "AudioWorkletNode", { value: Worklet, configurable: true });
  const track = { stop: vi.fn() };
  vi.stubGlobal("navigator", { mediaDevices: { getUserMedia: vi.fn(async () => ({ getTracks: () => [track] })) } });
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:test");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
  const input = document.createElement("textarea");
  const button = document.createElement("button");
  const error = vi.fn();
  const speech = new SpeechInput(input, button, error, vi.fn());
  return { input, button, speech, error, track };
}
async function start(speech: SpeechInput) {
  const opening = speech.toggle();
  const socket = Socket.instances.at(-1)!;
  socket.message({ type: "ready", sample_rate: 16000, format: "pcm_s16le" });
  await opening;
  return socket;
}
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); document.body.replaceChildren(); vi.useRealTimers(); });

it("resamples chunked audio to the same PCM16 as one whole buffer and flushes the tail", () => {
  const values = Float32Array.from({ length: 4410 }, (_, i) => Math.sin(i / 10));
  const whole = new Pcm16Resampler(44100).push(values, true);
  const converter = new Pcm16Resampler(44100);
  const chunks = [converter.push(values.slice(0, 333)), converter.push(values.slice(333, 3001)),
    converter.push(values.slice(3001)), converter.push(new Float32Array(), true)];
  const combined = new Uint8Array(chunks.reduce((sum, b) => sum + b.byteLength, 0));
  let offset = 0;
  for (const chunk of chunks) { combined.set(new Uint8Array(chunk), offset); offset += chunk.byteLength; }
  expect(combined.length).toBe(3200);
  expect([...combined]).toEqual([...new Uint8Array(whole)]);
  const clipped = new DataView(new Pcm16Resampler(16000).push(new Float32Array([-2, 2]), true));
  expect(clipped.getInt16(0, true)).toBe(-32768);
  expect(clipped.getInt16(2, true)).toBe(32767);
});

it("flushes partial worklet audio before acknowledging finish", () => {
  let Processor: { new(): { process(inputs: Float32Array[][]): boolean; port: { onmessage: (event: { data: string }) => void; postMessage: ReturnType<typeof vi.fn> } } };
  class Base { port = { postMessage: vi.fn(), onmessage: null }; }
  new Function("AudioWorkletProcessor", "registerProcessor", CAPTURE_WORKLET)(Base, (_: string, value: typeof Processor) => { Processor = value; });
  const capture = new Processor!();
  capture.process([[new Float32Array([0.5, 0.3]), new Float32Array([-0.5, 0.3])]]);
  capture.port.onmessage({ data: "flush" });
  expect([...capture.port.postMessage.mock.calls[0]![0]]).toEqual([0, expect.closeTo(0.3)]);
  expect(capture.port.postMessage.mock.calls[1]![0]).toBe("flushed");
});

it("uses local websocket, replaces interim segments, flushes audio and waits for done", async () => {
  const { speech, input, button, track } = setup();
  input.value = "prefix ";
  const socket = await start(speech);
  expect(socket.url).toContain("/speech");
  expect(button.getAttribute("aria-pressed")).toBe("true");
  socket.message({ type: "result", segment: 0, text: "你", final: false });
  socket.message({ type: "result", segment: 0, text: "你好", final: true });
  socket.message({ type: "result", segment: 1, text: "世界", final: false });
  expect(input.value).toBe("prefix 你好世界");
  Worklet.instances[0]!.port.onmessage!({ data: new Float32Array(4800).fill(0.25) });
  expect(socket.send.mock.calls[0]![0].byteLength).toBe(3200);
  let stopped = false;
  const stopping = speech.stop().then((ok) => { stopped = true; return ok; });
  await Promise.resolve(); await Promise.resolve();
  expect(stopped).toBe(false);
  expect(socket.send).toHaveBeenLastCalledWith(JSON.stringify({ type: "finish" }));
  socket.message({ type: "result", segment: 1, text: "世界。", final: true });
  socket.message({ type: "done" });
  expect(await stopping).toBe(true);
  expect(input.value).toBe("prefix 你好世界。");
  expect(track.stop).toHaveBeenCalled();
  expect(button.getAttribute("aria-pressed")).toBe("false");
  const late = socket.onmessage!;
  input.value = "";
  late({ data: JSON.stringify({ type: "result", segment: 1, text: "old", final: true }) });
  expect(input.value).toBe("");
});

it("times out finalization without sending incomplete text or keeping the microphone open", async () => {
  const { speech, track, error } = setup();
  await start(speech);
  vi.useFakeTimers();
  const stopping = speech.stop();
  let settled = false;
  void stopping.then(() => { settled = true; });
  await vi.advanceTimersByTimeAsync(11000);
  expect(settled).toBe(false); // CPU sentence refinement may exceed the old 10s limit.
  await vi.advanceTimersByTimeAsync(50000);
  expect(await stopping).toBe(false);
  expect(track.stop).toHaveBeenCalled();
  expect(error).toHaveBeenCalled();
});

it("releases resources on upstream error and keeps already recognized text", async () => {
  const { speech, input, track, error, button } = setup();
  const socket = await start(speech);
  socket.message({ type: "result", segment: 0, text: "保留文字", final: false });
  socket.message({ type: "error", message: "recognizer disconnected" });
  expect(input.value).toBe("保留文字");
  expect(track.stop).toHaveBeenCalled();
  expect(error).toHaveBeenCalledWith("recognizer disconnected");
  expect(button.getAttribute("aria-pressed")).toBe("false");
});

it("handles denied microphone permission without leaving the connection active", async () => {
  const { speech, error, button } = setup();
  vi.mocked(navigator.mediaDevices.getUserMedia).mockRejectedValue(new Error("permission denied"));
  await speech.toggle();
  expect(Socket.instances[0]!.close).toHaveBeenCalled();
  expect(button.getAttribute("aria-pressed")).toBe("false");
  expect(error).toHaveBeenCalledWith(expect.stringContaining("permission denied"));
});

it("finishes a stop requested while the microphone is still opening", async () => {
  const { speech, track } = setup();
  const opening = speech.toggle();
  const socket = Socket.instances[0]!;
  const stopping = speech.stop();
  socket.message({ type: "ready", sample_rate: 16000, format: "pcm_s16le" });
  await vi.waitFor(() => expect(socket.send).toHaveBeenCalledWith(JSON.stringify({ type: "finish" })));
  socket.message({ type: "done" });
  await opening;
  expect(await stopping).toBe(true);
  expect(track.stop).toHaveBeenCalled();
});

it.each(["button", "Enter", "microphone Enter"])("awaits final speech before %s sends and keeps normal sends working", async (method) => {
  const { speech, input, button } = setup();
  const app = document.createElement("div"), form = document.createElement("form"), send = document.createElement("button");
  form.id = "agent-form"; input.id = "agent-input"; button.dataset.action = "speech"; button.type = "button"; send.type = "submit";
  form.append(input, button, send); app.append(form); document.body.append(app);
  const source = readFileSync(new NodeURL("./main.ts", import.meta.url), "utf8");
  const submit = source.slice(source.indexOf("let speechSubmissionPending"), source.indexOf('// §7.1 (Q139): the wall'));
  const keydown = source.slice(source.indexOf('app.addEventListener("keydown"'), source.indexOf('app.addEventListener("change"'));
  const submitAgent = vi.fn();
  new Function("app", "document", "enabled", "submitAgent", "speechInput", "enterSendsAgentMessage", "state", transpile(submit + keydown))(
    app, document, () => true, submitAgent, speech, enterSendsAgentMessage, { features: { "agent.input.send": true } });
  const click = () => {
    if (method === "button") send.click();
    else (method === "Enter" ? input : button).dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
  };
  const socket = await start(speech);
  socket.message({ type: "result", segment: 0, text: "move", final: false });
  click(); click();
  expect(submitAgent).not.toHaveBeenCalled();
  await Promise.resolve(); await Promise.resolve();
  socket.message({ type: "result", segment: 0, text: "move cube", final: true });
  socket.message({ type: "done" });
  await vi.waitFor(() => expect(submitAgent).toHaveBeenCalledOnce());
  expect(submitAgent).toHaveBeenLastCalledWith("move cube");
  expect(input.readOnly).toBe(false);
  input.value = "normal"; click();
  await vi.waitFor(() => expect(submitAgent).toHaveBeenCalledTimes(2));
  expect(submitAgent).toHaveBeenLastCalledWith("normal");
});
