// The Claude Code provider's optional SDK. The board does not ship the Claude
// Agent SDK; this section shows its state and offers the install action. The
// action opens the pinned notice first, and the Install button stays disabled
// until the user acknowledges it. Only then does the board send the install
// request, carrying the digest of the notice it showed (runtime.installClaudeAgentSdk).
import * as RadixCheckbox from "@radix-ui/react-checkbox";
import { Check, Download } from "lucide-react";
import { type ReactElement, useCallback, useEffect, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { fetchClaudeAgentSdkStatus, installClaudeAgentSdk } from "@/runtime/runtime-config-query";
import type { RuntimeClaudeAgentSdkState, RuntimeClaudeAgentSdkStatus } from "@/runtime/types";

const STATE_LABEL: Record<RuntimeClaudeAgentSdkState, string> = {
	not_installed: "Not installed",
	installing: "Installing…",
	installed: "Installed",
	unavailable: "Installed, but unavailable",
};

export function claudeAgentSdkStateLabel(state: RuntimeClaudeAgentSdkState | undefined): string | null {
	return state ? STATE_LABEL[state] : null;
}

export function ClaudeAgentSdkNoticeDialog({
	open,
	onOpenChange,
	status,
	isInstalling,
	error,
	onConfirm,
}: {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	status: RuntimeClaudeAgentSdkStatus;
	isInstalling: boolean;
	error: string | null;
	onConfirm: (input: { acknowledgedNoticeSha256: string; sdkVersion: string | null }) => void;
}): ReactElement {
	const acknowledgeId = useId();
	const [acknowledged, setAcknowledged] = useState(false);
	const [sdkVersion, setSdkVersion] = useState(status.pinned.sdkVersion);

	useEffect(() => {
		if (open) {
			setAcknowledged(false);
			setSdkVersion(status.pinned.sdkVersion);
		}
	}, [open, status.pinned.sdkVersion]);

	const trimmedVersion = sdkVersion.trim();
	return (
		<Dialog open={open} onOpenChange={(next) => !isInstalling && onOpenChange(next)} contentClassName="max-w-2xl">
			<DialogHeader title={status.notice.title} icon={<Download size={16} />} />
			<DialogBody className="space-y-3">
				<p
					data-testid="claude-agent-sdk-notice"
					className="m-0 whitespace-pre-wrap text-[13px] leading-relaxed text-text-primary"
				>
					{status.notice.text}
				</p>
				<div className="grid gap-1">
					<label htmlFor={`${acknowledgeId}-version`} className="text-[12px] text-text-secondary">
						{status.sdkPackage} version (the lockfile pins {status.pinned.sdkVersion})
					</label>
					<input
						id={`${acknowledgeId}-version`}
						data-testid="claude-agent-sdk-version"
						value={sdkVersion}
						onChange={(event) => setSdkVersion(event.target.value)}
						disabled={isInstalling}
						className="h-8 w-48 rounded-md border border-border bg-surface-2 px-2 text-[13px] text-text-primary focus:border-border-focus focus:outline-none"
					/>
				</div>
				<label
					htmlFor={acknowledgeId}
					className="flex cursor-pointer items-start gap-2 text-[13px] text-text-primary"
				>
					<RadixCheckbox.Root
						id={acknowledgeId}
						data-testid="claude-agent-sdk-acknowledge"
						checked={acknowledged}
						disabled={isInstalling}
						onCheckedChange={(checked) => setAcknowledged(checked === true)}
						className="mt-0.5 flex h-4 w-4 shrink-0 cursor-pointer items-center justify-center rounded border border-border bg-surface-2 data-[state=checked]:border-accent data-[state=checked]:bg-accent disabled:cursor-default disabled:opacity-40"
					>
						<RadixCheckbox.Indicator>
							<Check size={12} className="text-white" />
						</RadixCheckbox.Indicator>
					</RadixCheckbox.Root>
					<span>
						I have read this notice. I obtain the Claude Agent SDK myself, under Anthropic's terms; the board's
						authors neither license nor warrant it.
					</span>
				</label>
				{error ? (
					<p data-testid="claude-agent-sdk-error" className="m-0 whitespace-pre-wrap text-[12px] text-status-red">
						{error}
					</p>
				) : null}
			</DialogBody>
			<DialogFooter>
				<Button variant="default" size="sm" disabled={isInstalling} onClick={() => onOpenChange(false)}>
					Cancel
				</Button>
				<Button
					variant="primary"
					size="sm"
					data-testid="claude-agent-sdk-confirm-install"
					disabled={!acknowledged || isInstalling || trimmedVersion.length === 0}
					icon={isInstalling ? <Spinner size={12} /> : <Download size={14} />}
					onClick={() => {
						if (!acknowledged) {
							return;
						}
						onConfirm({
							acknowledgedNoticeSha256: status.notice.sha256,
							sdkVersion: trimmedVersion === status.pinned.sdkVersion ? null : trimmedVersion,
						});
					}}
				>
					{isInstalling ? "Installing…" : "Install"}
				</Button>
			</DialogFooter>
		</Dialog>
	);
}

/**
 * The Claude Code provider's install state and its install action. Renders
 * nothing until the runtime answers; never blocks the surrounding settings.
 */
export function ClaudeAgentSdkInstallSection({
	workspaceId,
	open,
	onInstalled,
}: {
	workspaceId: string | null;
	open: boolean;
	onInstalled?: () => void;
}): ReactElement | null {
	const [status, setStatus] = useState<RuntimeClaudeAgentSdkStatus | null>(null);
	const [isNoticeOpen, setIsNoticeOpen] = useState(false);
	const [isInstalling, setIsInstalling] = useState(false);
	const [error, setError] = useState<string | null>(null);

	useEffect(() => {
		if (!open) {
			return;
		}
		let cancelled = false;
		// Any failure, synchronous or not, leaves the section hidden; it never blocks settings.
		void Promise.resolve()
			.then(() => fetchClaudeAgentSdkStatus(workspaceId))
			.then((next) => {
				if (!cancelled) {
					setStatus(next);
				}
			})
			.catch(() => {
				if (!cancelled) {
					setStatus(null);
				}
			});
		return () => {
			cancelled = true;
		};
	}, [open, workspaceId]);

	const handleConfirm = useCallback(
		(input: { acknowledgedNoticeSha256: string; sdkVersion: string | null }) => {
			setIsInstalling(true);
			setError(null);
			void Promise.resolve()
				.then(() => installClaudeAgentSdk(workspaceId, input))
				.then((response) => {
					setStatus(response.status);
					if (response.ok) {
						setIsNoticeOpen(false);
						onInstalled?.();
						return;
					}
					setError(response.error ?? "The install did not complete.");
				})
				.catch((caught: unknown) => {
					setError(caught instanceof Error ? caught.message : String(caught));
				})
				.finally(() => {
					setIsInstalling(false);
				});
		},
		[onInstalled, workspaceId],
	);

	if (!status) {
		return null;
	}

	const installed = status.state === "installed";
	return (
		<div data-testid="claude-agent-sdk-section" className="mt-2">
			<div className="flex flex-wrap items-center gap-2">
				<p className="m-0 text-[13px] text-text-primary">Claude Code provider (Claude Agent SDK)</p>
				<span
					data-testid="claude-agent-sdk-state"
					data-state={status.state}
					className={
						installed
							? "rounded bg-status-green/10 px-1.5 py-0.5 text-[11px] text-status-green"
							: "rounded bg-surface-3 px-1.5 py-0.5 text-[11px] text-text-secondary"
					}
				>
					{STATE_LABEL[status.state]}
				</span>
				{installed ? null : (
					<Button
						variant="default"
						size="sm"
						data-testid="claude-agent-sdk-install"
						icon={<Download size={14} />}
						disabled={status.state === "installing" || isInstalling}
						onClick={() => {
							setError(null);
							setIsNoticeOpen(true);
						}}
					>
						{status.installAction.label}…
					</Button>
				)}
			</div>
			<p className="m-0 mt-1 text-[12px] text-text-secondary">
				{installed && status.installed
					? `${status.sdkPackage} ${status.installed.sdkVersion} (adapter ${status.providerPackage} ${status.installed.providerVersion}), installed by you under ${status.installRoot}.`
					: "The board does not include the Claude Agent SDK. Every other provider and board function works without it."}
			</p>
			{status.provider.error && !installed ? (
				<p className="m-0 mt-1 whitespace-pre-wrap text-[12px] text-status-red">{status.provider.error}</p>
			) : null}
			<ClaudeAgentSdkNoticeDialog
				open={isNoticeOpen}
				onOpenChange={setIsNoticeOpen}
				status={status}
				isInstalling={isInstalling}
				error={error}
				onConfirm={handleConfirm}
			/>
		</div>
	);
}
