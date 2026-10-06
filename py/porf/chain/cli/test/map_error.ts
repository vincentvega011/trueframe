// Test helper (run by py/tests/test_chain.py): prints normalizeError() for a synthetic failure message,
// using whatever IDL PORF_IDL points at. Proves error names come from the IDL at runtime.
import { normalizeError } from "../src/errors";
import { loadIdl } from "../src/util";

normalizeError(new Error(process.argv[2] ?? ""), loadIdl()).then((e) => console.log(JSON.stringify(e)));
