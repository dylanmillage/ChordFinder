/* Runs the browser import module under Node so its output can be compared
 * against pipeline/import_sheet.py. Used by cloud/compare_import.py. */
const fs = require("fs");
global.window = {};
require("../frontend/import-sheet.js");

const [sheetPath, wordsPath, chordsPath] = process.argv.slice(2);
const text = fs.readFileSync(sheetPath, "utf8");
const words = JSON.parse(fs.readFileSync(wordsPath, "utf8"));
const chords = JSON.parse(fs.readFileSync(chordsPath, "utf8"));

const { segments, report } = window.ImportSheet.importSheet(text, words, chords);
process.stdout.write(JSON.stringify({
  segments,
  matched_lines: report.matched_lines,
  chords_placed: report.chords_placed,
  key_offset: report.key_offset,
  key_confidence: report.key_confidence,
  kept_detected: report.kept_detected,
  unmatched: report.unmatched,
}));
