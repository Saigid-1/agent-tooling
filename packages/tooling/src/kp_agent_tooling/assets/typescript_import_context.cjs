// Syntax context from the same TypeScript compiler API used by the OPS extractor.
// Declaration resolution remains with SCIP; this does not infer filesystem paths.
const fs = require('node:fs');
const ts = require(process.argv[2]);
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const sf = ts.createSourceFile(input.path, input.text, ts.ScriptTarget.Latest, true);
const rows = [];
const line = n => sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1;
function add(node, name, imported, module, kind, typeOnly) {
  rows.push({local_name:name, imported_name:imported, module, kind,
    type_only:!!typeOnly, import_line:line(node), lookup_line:line(node), binding_status:'unresolved'});
}
for (const node of sf.statements) {
  if (ts.isImportDeclaration(node)) {
    const module = node.moduleSpecifier.text, c = node.importClause;
    if (!c) { add(node, null, null, module, 'side_effect', false); continue; }
    if (c.name) add(c.name,c.name.text,'default',module,'import',c.isTypeOnly);
    const b=c.namedBindings;
    if (b && ts.isNamespaceImport(b)) add(b.name,b.name.text,'*',module,'namespace',c.isTypeOnly);
    if (b && ts.isNamedImports(b)) for (const e of b.elements)
      add(e,e.name.text,(e.propertyName||e.name).text,module,'import',c.isTypeOnly||e.isTypeOnly);
  } else if (ts.isExportDeclaration(node) && node.moduleSpecifier) {
    const module=node.moduleSpecifier.text;
    if (node.exportClause && ts.isNamedExports(node.exportClause)) for (const e of node.exportClause.elements)
      add(e,e.name.text,(e.propertyName||e.name).text,module,'re_export',node.isTypeOnly||e.isTypeOnly);
    else add(node, node.exportClause?.name?.text || '*','*',module,'re_export',node.isTypeOnly);
  } else if (ts.isImportEqualsDeclaration(node) && ts.isExternalModuleReference(node.moduleReference)) {
    add(node.name,node.name.text,'*',node.moduleReference.expression?.text,'import_equals',node.isTypeOnly);
  }
}
process.stdout.write(JSON.stringify({status:sf.parseDiagnostics.length?'unavailable':rows.length?'ok':'no_results',
 reason:sf.parseDiagnostics.length?'source_parse_failed':null,
 parser:{name:'typescript',version:ts.version,node:process.version},
 imports:sf.parseDiagnostics.length?[]:rows.slice(0,24),omitted:Math.max(0,rows.length-24),
 coverage:'top-level import and re-export syntax; dynamic imports and require calls not covered'}));
