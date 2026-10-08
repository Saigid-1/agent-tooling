import { createHash } from "node:crypto";
import { openSync, closeSync, fstatSync, readFileSync, constants } from "node:fs";
import { projectDeskTemporal } from "../core/desk-temporal";
export function readDeskTemporal(path: string, tenant: string, session: string) {
	const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
	try {
		const info = fstatSync(fd);
		if (!info.isFile() || info.size > 2000000)
			throw new Error("Desk history must be a regular export of at most 2 MB");
		const bytes = readFileSync(fd);
		if (bytes.length > 2000000) throw new Error("Desk history exceeds 2 MB");
		return {
			...projectDeskTemporal(JSON.parse(bytes.toString("utf8")), tenant, session),
			export_sha256: createHash("sha256").update(bytes).digest("hex"),
		};
	} finally {
		closeSync(fd);
	}
}
