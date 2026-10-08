// Decode using the pinned indexer's generated protocol implementation.
const fs = require('fs');
const path = require('path');
const [toolchain, file] = process.argv.slice(2);
if (fs.statSync(file).size > 64 * 1024 * 1024) throw Error('SCIP file exceeds 64 MiB');
const {scip} = require(path.join(path.resolve(toolchain), 'node_modules/@sourcegraph/scip-typescript/dist/src/scip.js'));
process.stdout.write(JSON.stringify(scip.Index.deserializeBinary(fs.readFileSync(file)).toObject()));
