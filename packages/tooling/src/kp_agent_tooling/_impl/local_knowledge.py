"""Optional in-process knowledge provider for pinned platform and SCIP reads.

The provider accepts a separate, operator-owned source catalog. Maintained-document
search belongs to a different composition and is deliberately not advertised here.
"""
import json
from pathlib import Path

# Operations this provider serves once a catalog is configured.
OPERATIONS = ('knowledge.platform', 'knowledge.symbol')


class LocalKnowledgeProvider:
    def __init__(self, catalog_path):
        self.config = json.loads(Path(catalog_path).read_text())
        for key in ("repositories", "platforms", "scip_indexes"):
            if key not in self.config or not isinstance(self.config[key], dict):
                raise ValueError("local knowledge catalog requires " + key)

    def tools(self):
        anchors = list(self.config["platforms"])
        members = list(self.config["repositories"])
        platform_members = sorted(set(anchors) | {key for key in members if any(
            key in manifest["sources"] for manifest in self.config["platforms"].values())})
        return [
            {"name": "knowledge.platform", "description": "Inspect explicitly configured platform source pins and selected committed artifacts. Checkout and installed identities are separate from runtime deployment.",
             "inputSchema": {"type": "object", "properties": {"repo_key": {"type": "string", "enum": platform_members}}, "required": ["repo_key"], "additionalProperties": False}},
            {"name": "knowledge.symbol", "description": "Resolve a pinned path and line through locally published SCIP indexes. Static candidates are not executed calls.",
             "inputSchema": {"type": "object", "properties": {"repo_key": {"type": "string", "enum": members}, "target_revision": {"type": "string", "pattern": "^[0-9a-f]{40}$|^[0-9a-f]{64}$"}, "path": {"type": "string"}, "line": {"type": "integer", "minimum": 1}},
                             "required": ["repo_key", "target_revision", "path", "line"], "additionalProperties": False}},
        ]

    def call(self, name, args):
        if name == "knowledge.platform":
            from kp_agent_tooling._impl.platform_snapshot import inspect_platform
            requested_repo = args["repo_key"]
            anchors = ([requested_repo] if requested_repo in self.config["platforms"] else
                       [key for key, value in self.config["platforms"].items() if requested_repo in value["sources"]])
            if len(anchors) != 1:
                raise ValueError("repository requires one unambiguous configured platform")
            anchor = anchors[0]
            manifest = self.config["platforms"][anchor]
            checkouts = {key: self.config["repositories"][key]["path"] for key in manifest["sources"]}
            result = inspect_platform(manifest, checkouts)
            for observation in result["checkouts"].values():
                observation.pop("path", None)
            result["platform_anchor"] = anchor
            result["requested_repo_key"] = requested_repo
            result["source_revisions"] = {key: row["revision"] for key, row in result["snapshot"]["sources"].items()}
            return {"schema_version": "ops.knowledge.v1", "operation": "platform", "repo_key": requested_repo,
                    "source_revision": result["source_revisions"].get(requested_repo), "status": result["status"],
                    "data": result, "omitted": 0, "limitations": result["limitations"]}
        if name == "knowledge.symbol":
            from kp_agent_tooling._impl.service.scip_entry import symbol_report
            repo_key = args["repo_key"]
            anchors = [key for key, manifest in self.config["platforms"].items()
                       if repo_key in manifest["sources"] and manifest["sources"][repo_key]["revision"] == args["target_revision"]]
            if len(anchors) != 1:
                return {"schema_version": "ops.knowledge.v1", "operation": "symbol", "repo_key": repo_key,
                        "source_revision": args["target_revision"], "status": "partial", "data": {"results": [], "gaps": ["target_not_in_unique_platform_snapshot"]}, "omitted": 0}
            return symbol_report(self.config, anchors[0], args["target_revision"], args["path"], args["line"], repo_key=repo_key)
        raise ValueError("unsupported local knowledge operation")
