// Stands in for `kanban hooks ingest` inside the image: the board plugin's hook command runs this
// file, which appends its arguments, the decoded metadata and the hook task id to O1_HOOK_LOG.
import { appendFileSync } from "node:fs";

const args = process.argv.slice(2);
const index = args.indexOf("--metadata-base64");
let metadata = null;
if (index >= 0) {
	try {
		metadata = JSON.parse(Buffer.from(args[index + 1] ?? "", "base64").toString("utf8"));
	} catch {
		metadata = "undecodable";
	}
}
const event = args[args.indexOf("--event") + 1] ?? null;
appendFileSync(
	process.env.O1_HOOK_LOG ?? "/dev/null",
	`${JSON.stringify({ event, metadata, taskId: process.env.KANBAN_HOOK_TASK_ID ?? null })}\n`,
);
