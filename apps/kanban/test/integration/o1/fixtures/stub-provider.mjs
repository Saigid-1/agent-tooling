// A loopback OpenAI-compatible model for the O1 A1 check: no model, no key, no network.
// node stub-provider.mjs <port> <log file>
// The first user message names a case ("CASE:bash", "CASE:webfetch", "CASE:external PATH:<file to the end of the line>").
// The stub answers that case with one tool call, then with text once a tool result is present.
// Every request is logged as one JSON line; GET /page counts web fetches; /npm answers 404 at
// once, so OpenCode's background dependency step fails fast instead of waiting on a closed network.
import { appendFileSync } from "node:fs";
import { createServer } from "node:http";

const port = Number(process.argv[2]);
const logFile = process.argv[3];

function log(entry) {
	appendFileSync(logFile, `${JSON.stringify({ time: Date.now(), ...entry })}\n`);
}

function textOf(message) {
	if (typeof message?.content === "string") return message.content;
	if (Array.isArray(message?.content)) return message.content.map((part) => part?.text ?? "").join("\n");
	return "";
}

function toolCall(name, args) {
	return { name, args };
}

function callFor(caseName, target) {
	if (caseName === "bash") return toolCall("bash", { command: "touch bash-marker.txt", description: "Touch a marker" });
	if (caseName === "webfetch") return toolCall("webfetch", { url: `http://127.0.0.1:${port}/page`, format: "text" });
	if (caseName === "external") return toolCall("write", { filePath: target, content: "outside\n" });
	if (caseName === "edit") return toolCall("write", { filePath: target, content: "inside\n" });
	return null;
}

function chunk(model, choice) {
	return { id: "chatcmpl-o1", object: "chat.completion.chunk", created: Math.floor(Date.now() / 1000), model, choices: [choice] };
}

function stream(res, chunks) {
	res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache" });
	for (const item of chunks) res.write(`data: ${JSON.stringify(item)}\n\n`);
	res.end("data: [DONE]\n\n");
}

const usage = { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 };

createServer((req, res) => {
	let raw = "";
	req.on("data", (data) => {
		raw += data;
	});
	req.on("end", () => {
		if (req.url?.startsWith("/npm")) {
			res.writeHead(404, { "content-type": "application/json" });
			res.end("{}");
			return;
		}
		if (req.url?.startsWith("/page")) {
			log({ kind: "page" });
			res.writeHead(200, { "content-type": "text/plain" });
			res.end("o1 page\n");
			return;
		}
		let body = {};
		try {
			body = JSON.parse(raw || "{}");
		} catch {
			body = {};
		}
		const messages = Array.isArray(body.messages) ? body.messages : [];
		const tools = Array.isArray(body.tools) ? body.tools.map((tool) => tool?.function?.name).filter(Boolean) : [];
		const first = textOf(messages.find((message) => message?.role === "user"));
		const caseName = /CASE:([a-z]+)/.exec(first)?.[1] ?? null;
		// The target is the rest of the line, so a path with spaces survives.
		const target = /PATH:(.+)$/m.exec(first)?.[1]?.trim() ?? null;
		const last = messages[messages.length - 1];
		log({ kind: "chat", case: caseName, lastRole: last?.role ?? null, tools });
		const call = tools.length > 0 && caseName && last?.role !== "tool" ? callFor(caseName, target) : null;
		if (!call) {
			stream(res, [
				chunk(body.model, { index: 0, delta: { role: "assistant", content: last?.role === "tool" ? "done" : "o1" }, finish_reason: null }),
				{ ...chunk(body.model, { index: 0, delta: {}, finish_reason: "stop" }), usage },
			]);
			return;
		}
		const toolCallDelta = { index: 0, id: `call_${Date.now()}`, type: "function", function: { name: call.name, arguments: JSON.stringify(call.args) } };
		stream(res, [
			chunk(body.model, { index: 0, delta: { role: "assistant", tool_calls: [toolCallDelta] }, finish_reason: null }),
			{ ...chunk(body.model, { index: 0, delta: {}, finish_reason: "tool_calls" }), usage },
		]);
	});
}).listen(port, "127.0.0.1", () => log({ kind: "listening" }));
