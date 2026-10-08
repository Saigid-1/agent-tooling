import type { RuntimeBoardData } from "./api-contract";
export function isProjectedTask(id: string): boolean {
	return id.trim().startsWith("obs_");
}
export function requireDispatchableTask(id: string): void {
	if (isProjectedTask(id)) throw new Error("Projected work is read-only; continue in its source chat");
}
export function preserveProjectedCards(before: RuntimeBoardData, after: RuntimeBoardData): void {
	const selected = (board: RuntimeBoardData) =>
		board.columns
			.flatMap((c) => c.cards.filter((t) => isProjectedTask(t.id)).map((t) => ({ column: c.id, card: t })))
			.sort((a, b) => a.card.id.localeCompare(b.card.id));
	if (JSON.stringify(selected(before)) !== JSON.stringify(selected(after)))
		throw new Error("Projected cards can only be updated by captured observations");
}
