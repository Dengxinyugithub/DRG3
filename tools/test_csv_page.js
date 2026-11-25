// 从页面里抽出解码/清洗函数，用 Node 跑真实字节（UTF-8/BOM/GBK/sep/空行）。
const fs = require("fs");
const path = require("path");

const htmlPath = process.argv[2] || path.join(__dirname, "..", "web", "index.html");
const html = fs.readFileSync(htmlPath, "utf8");

function extract(name) {
  const start = html.indexOf("function " + name + "(");
  if (start < 0) throw new Error("找不到函数 " + name);
  let i = html.indexOf("{", start);
  let depth = 0;
  for (; i < html.length; i++) {
    if (html[i] === "{") depth++;
    else if (html[i] === "}") {
      depth--;
      if (depth === 0) return html.slice(start, i + 1);
    }
  }
  throw new Error("函数未闭合 " + name);
}

const code = extract("decodeCsvBuffer") + "\n" + extract("normalizeCsvText") +
  "\nreturn { decodeCsvBuffer: decodeCsvBuffer, normalizeCsvText: normalizeCsvText };";
const api = new Function(code)();

const HEADER = "patient_id,age,age_unit,gender,birth_weight_g,primary_diagnosis_code,primary_operation_code";
const base = path.join(__dirname, "..", "tests", "fixtures");
const cases = [
  ["utf8.csv", "UTF-8"],
  ["utf8bom.csv", "UTF-8（带 BOM）"],
  ["gbk.csv", "GB18030/GBK"],
  ["sep.csv", "UTF-8"],
  ["blank.csv", "UTF-8"],
];

let failed = 0;
for (const [file, wantEncoding] of cases) {
  const buf = fs.readFileSync(path.join(base, file));
  const ab = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
  const decoded = api.decodeCsvBuffer(ab);
  const text = api.normalizeCsvText(decoded.text);
  const header = text.split("\r\n")[0];
  const okEnc = decoded.encoding === wantEncoding;
  const okHead = header === HEADER;
  const noBom = !text.startsWith("\ufeff");
  const chineseOk = file !== "gbk.csv" || text.indexOf("脑梗死") >= 0;
  const pass = okEnc && okHead && noBom && chineseOk;
  console.log(`${pass ? "PASS" : "FAIL"}  ${file.padEnd(12)} encoding=${decoded.encoding.padEnd(18)} headerOK=${okHead} chineseOK=${chineseOk}`);
  if (!pass) failed++;
}

const semiHeader = "patient_id;age;gender";
console.log(`${semiHeader.indexOf(",") === -1 ? "PASS" : "FAIL"}  分号表头会被判为非逗号分隔`);

console.log(failed ? `失败 ${failed} 项` : "全部通过");
process.exit(failed ? 1 : 0);
