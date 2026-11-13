import { Pcm16Resampler } from "./speech_audio";

export const CAPTURE_WORKLET = `class Capture extends AudioWorkletProcessor {
  constructor() { super(); this.samples = []; this.active = true;
    this.port.onmessage = e => { if (e.data === 'flush') {
      if (this.samples.length) this.port.postMessage(new Float32Array(this.samples));
      this.samples = []; this.active = false; this.port.postMessage('flushed');
    }};
  }
  process(inputs) {
    if (!this.active) return true;
    const channels = inputs[0];
    if (!channels || !channels.length) return true;
    for (let i = 0; i < channels[0].length; i++) {
      let sample = 0; for (const channel of channels) sample += channel[i];
      this.samples.push(sample / channels.length);
      if (this.samples.length === 2048) {
        this.port.postMessage(new Float32Array(this.samples)); this.samples = [];
      }
    }
    return true;
  }
} registerProcessor('speech-capture', Capture);`;

interface Session {
  socket: WebSocket;
  prefix: string;
  segments: Map<number, string>;
  stream?: MediaStream;
  context?: AudioContext;
  node?: AudioWorkletNode;
  source?: MediaStreamAudioSourceNode;
  resampler?: Pcm16Resampler;
  flush?: () => void;
  closing: boolean;
  ended: boolean;
  timer: ReturnType<typeof setTimeout>;
  ready: Promise<void>;
  resolveReady: () => void;
  rejectReady: (error: Error) => void;
  done: Promise<boolean>;
  resolveDone: (ok: boolean) => void;
}

export class SpeechInput {
  private session: Session | null = null;
  constructor(private input: HTMLTextAreaElement, private button: HTMLButtonElement,
              private error: (message: string) => void, private listening: () => void) { this.render(false); }

  private render(active: boolean, finishing = false): void {
    this.button.setAttribute("aria-pressed", String(active));
    this.button.setAttribute("aria-busy", String(finishing));
    const label = finishing ? "正在完成语音识别…" : active ? "停止语音输入（正在聆听）" : "开始语音输入";
    this.button.title = label;
    this.button.setAttribute("aria-label", label);
  }

  private end(session: Session, ok: boolean, message?: string): void {
    if (session.ended) return;
    session.ended = true;
    clearTimeout(session.timer);
    session.rejectReady(new Error(message ?? "语音识别已结束"));
    session.flush?.();
    session.source?.disconnect();
    session.node?.disconnect();
    session.stream?.getTracks().forEach((track) => track.stop());
    void session.context?.close().catch(() => undefined);
    session.socket.close();
    if (this.session === session) { this.session = null; this.render(false); }
    if (message) this.error(message);
    session.resolveDone(ok);
  }

  async toggle(): Promise<void> {
    if (this.session) { await this.stop(); return; }
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
      this.error("语音输入需要支持麦克风的浏览器及 HTTPS（或可信来源）。"); return;
    }
    let resolveReady!: () => void, rejectReady!: (error: Error) => void, resolveDone!: (ok: boolean) => void;
    const ready = new Promise<void>((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
    void ready.catch(() => undefined);
    const done = new Promise<boolean>((resolve) => { resolveDone = resolve; });
    const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/speech`);
    const session: Session = { socket, prefix: this.input.value, segments: new Map(), closing: false, ended: false,
      ready, resolveReady, rejectReady, done, resolveDone,
      timer: setTimeout(() => this.end(session, false, "语音服务连接超时，请重试。"), 10000) };
    this.session = session;
    this.render(true);
    socket.onmessage = (event) => {
      if (this.session !== session || session.ended) return;
      try {
        const message = JSON.parse(String(event.data));
        if (message.type === "ready") {
          if (message.sample_rate !== 16000 || message.format !== "pcm_s16le") throw new Error("语音服务音频格式不兼容。");
          session.resolveReady();
        } else if (message.type === "result") {
          if (!Number.isInteger(message.segment) || message.segment < 0 || typeof message.text !== "string") throw new Error("语音服务返回无效结果。");
          session.segments.set(message.segment, message.text);
          this.input.value = session.prefix + [...session.segments].sort(([a], [b]) => a - b).map(([, text]) => text).join("");
        } else if (message.type === "done") this.end(session, true);
        else if (message.type === "error") this.end(session, false, String(message.message));
      } catch (error) { this.end(session, false, (error as Error).message); }
    };
    socket.onerror = () => this.end(session, false, "语音连接失败，请检查 Gateway 与语音服务。");
    socket.onclose = () => this.end(session, false, "语音连接中断，已保留识别文字，请检查后重试。");
    try {
      session.context = new AudioContext();
      await session.context.resume();
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false });
      if (session.ended) { stream.getTracks().forEach((track) => track.stop()); return; }
      session.stream = stream;
      await session.ready;
      if (session.ended) return;
      const url = URL.createObjectURL(new Blob([CAPTURE_WORKLET], { type: "text/javascript" }));
      try { await session.context.audioWorklet.addModule(url); }
      finally { URL.revokeObjectURL(url); }
      if (session.ended) return;
      session.resampler = new Pcm16Resampler(session.context.sampleRate);
      session.node = new AudioWorkletNode(session.context, "speech-capture");
      session.node.port.onmessage = (event) => {
        if (session.ended) return;
        if (event.data === "flushed") { session.flush?.(); return; }
        if (!(event.data instanceof Float32Array)) return;
        const pcm = session.resampler!.push(event.data);
        if (socket.bufferedAmount > 256000) { this.end(session, false, "语音网络传输过慢，已停止录音并保留文字。"); return; }
        if (pcm.byteLength && socket.readyState === WebSocket.OPEN) socket.send(pcm);
      };
      session.source = session.context.createMediaStreamSource(stream);
      session.source.connect(session.node);
      session.node.connect(session.context.destination);
      clearTimeout(session.timer);
      if (session.closing) await this.finish(session);
      else this.listening();
    } catch (error) {
      if (!session.ended) this.end(session, false, `语音输入失败：${(error as Error).message}`);
    }
  }

  private async finish(session: Session): Promise<void> {
    if (!session.node || session.ended) return;
    this.render(false, true);
    const flushed = new Promise<void>((resolve) => { session.flush = resolve; });
    session.node.port.postMessage("flush");
    const flushTimeout = setTimeout(() => this.end(session, false, "录音收尾超时，已保留识别文字。"), 2000);
    await flushed;
    clearTimeout(flushTimeout);
    if (session.ended) return;
    session.stream?.getTracks().forEach((track) => track.stop());
    session.source?.disconnect();
    session.node.disconnect();
    const tail = session.resampler!.push(new Float32Array(), true);
    if (tail.byteLength) session.socket.send(tail);
    session.socket.send(JSON.stringify({ type: "finish" }));
    session.timer = setTimeout(() => this.end(session, false, "语音识别收尾超时，文字已保留，请重试发送。"), 60000);
  }

  async stop(): Promise<boolean> {
    const session = this.session;
    if (!session) return true;
    if (!session.closing) {
      session.closing = true;
      this.render(false, true);
      try { await this.finish(session); }
      catch (error) { this.end(session, false, `语音收尾失败：${(error as Error).message}`); }
    }
    return session.done;
  }
}
