// Rebuild the submission-format draft from the Markdown source; no content edits here.
const fs = require('fs');
const path = require('path');
const {Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  HeadingLevel, WidthType, BorderStyle, ShadingType, PageOrientation,
  Footer, PageNumber, AlignmentType} = require('docx');

const root = path.resolve(__dirname, '../../..');
const source = path.join(root, '06_测试与实测报告.md');
const target = path.join(root, '06_测试与实测报告.docx');
const lines = fs.readFileSync(source, 'utf8').replace(/\r\n/g, '\n').split('\n');
const font = 'Microsoft YaHei';
const contentWidth = 15138;
const border = {style: BorderStyle.SINGLE, size: 4, color: 'D7DCE2'};
const borders = {top: border, bottom: border, left: border, right: border};

function plain(value) {
  return value.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '$1（$2）')
    .replace(/\*\*/g, '').replace(/`/g, '');
}
function paragraph(value, options = {}) {
  return new Paragraph({spacing: {after: 130, line: 330}, ...options,
    children: [new TextRun({text: plain(value), font, size: options.small ? 17 : 20})]});
}
function table(raw) {
  const rows = raw.filter(line => !/^\|[\s:|-]+\|$/.test(line))
    .map(line => line.slice(1, -1).split('|').map(cell => plain(cell.trim())));
  const columns = rows[0].length;
  const widths = columns === 4 ? [2350, 3850, 6150, 2788] :
    columns === 3 ? [2200, 7100, 5838] : Array(columns).fill(Math.floor(contentWidth / columns));
  widths[widths.length - 1] += contentWidth - widths.reduce((a, b) => a + b, 0);
  return new Table({width: {size: contentWidth, type: WidthType.DXA}, columnWidths: widths,
    rows: rows.map((cells, rowIndex) => new TableRow({tableHeader: rowIndex === 0,
      children: cells.map((value, index) => new TableCell({borders,
        width: {size: widths[index], type: WidthType.DXA},
        shading: rowIndex === 0 ? {fill: 'E9EFF5', type: ShadingType.CLEAR} : undefined,
        margins: {top: 95, bottom: 95, left: 105, right: 105},
        children: [paragraph(value, {small: true, spacing: {after: 0, line: 245}})]}))}))});
}

const children = [];
for (let index = 0; index < lines.length;) {
  const line = lines[index].trim();
  if (!line) { index++; continue; }
  if (line.startsWith('|')) {
    const group = [];
    while (index < lines.length && lines[index].trim().startsWith('|')) group.push(lines[index++].trim());
    children.push(table(group));
    continue;
  }
  if (line.startsWith('# ')) {
    children.push(new Paragraph({heading: HeadingLevel.TITLE, spacing: {after: 250},
      children: [new TextRun({text: plain(line.slice(2)), font, size: 34, bold: true})]}));
  } else if (line.startsWith('## ')) {
    children.push(new Paragraph({heading: HeadingLevel.HEADING_1, keepWithNext: true,
      spacing: {before: 280, after: 140},
      children: [new TextRun({text: plain(line.slice(3)), font, size: 26, bold: true})]}));
  } else {
    children.push(paragraph(line.startsWith('> ') ? line.slice(2) : line));
  }
  index++;
}

const doc = new Document({creator: 'AI assisted course project', title: 'T22测试与实测报告（验收工作稿）',
  styles: {default: {document: {run: {font, size: 20}}}, paragraphStyles: [
    {id: 'Heading1', name: 'Heading 1', basedOn: 'Normal', next: 'Normal', quickFormat: true,
      run: {font, size: 26, bold: true}, paragraph: {outlineLevel: 0}},
  ]},
  sections: [{properties: {page: {size: {width: 11906, height: 16838,
    orientation: PageOrientation.LANDSCAPE},
    margin: {top: 850, right: 850, bottom: 850, left: 850}}},
    footers: {default: new Footer({children: [new Paragraph({alignment: AlignmentType.CENTER,
      children: [new TextRun({text: 'T22 验收工作稿 · 第 ', font, size: 16}),
        new TextRun({children: [PageNumber.CURRENT], font, size: 16}),
        new TextRun({text: ' 页', font, size: 16})]})]})},
    children}]});
Packer.toBuffer(doc).then(buffer => fs.writeFileSync(target, buffer));
