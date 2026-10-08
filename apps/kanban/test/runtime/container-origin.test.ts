import { afterEach, describe, expect, it, vi } from "vitest";
import { evaluateCors, evaluateHost, getAllowedHostHeaders, getPublicOrigin } from "../../src/server/middleware";

afterEach(() => vi.unstubAllEnvs());
describe("explicit container browser origin", () => {
	it("allows only the configured mapped host and browser origin", () => {
		vi.stubEnv("KANBAN_PUBLIC_ORIGIN", "http://127.0.0.1:3487");
		expect(getPublicOrigin()).toBe("http://127.0.0.1:3487");
		expect(evaluateHost({hostHeader:"127.0.0.1:3487",allowedHosts:getAllowedHostHeaders()}).kind).toBe("allow");
		expect(evaluateHost({hostHeader:"attacker.invalid:3487",allowedHosts:getAllowedHostHeaders()}).kind).toBe("reject");
		expect(evaluateCors({method:"POST",originHeader:"http://attacker.invalid",allowedOrigin:getPublicOrigin()}).kind).toBe("reject");
	});
	it.each(["*", "http://0.0.0.0:3487", "https://user:pass@example.com", "https://example.com/path"])("refuses ambiguous or credential-bearing origin %s", (value) => {
		vi.stubEnv("KANBAN_PUBLIC_ORIGIN",value);
		expect(() => getPublicOrigin()).toThrow();
	});
});
