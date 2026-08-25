#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';

function fail(message, code = 1) {
  console.error(`error: ${message}`);
  process.exit(code);
}

function usage() {
  console.error('usage: node scripts/export-archify-svg.mjs <input.html> <output.svg>');
  process.exit(2);
}

function firstMatch(text, regex, label) {
  const match = text.match(regex);
  if (!match) fail(`unable to locate ${label} in Archify HTML`);
  return match[1];
}

function extractThemeVariables(css, selectorPattern, label) {
  const body = firstMatch(css, selectorPattern, `${label} theme variables`);
  const variables = body
    .split(';')
    .map((entry) => entry.trim())
    .filter((entry) => entry.startsWith('--'))
    .map((entry) => `${entry};`)
    .join(' ');
  if (!variables) fail(`${label} theme contains no CSS variables`);
  return variables;
}

function ensureSvgAttribute(openTag, name, value) {
  const attribute = new RegExp(`\\s${name}\\s*=`, 'i');
  if (attribute.test(openTag)) return openTag;
  return openTag.replace(/>$/, ` ${name}="${value}">`);
}

function standaloneSvg(html) {
  const css = firstMatch(html, /<style>([\s\S]*?)<\/style>/i, 'Archify stylesheet')
    .replace(/\/\*[\s\S]*?\*\//g, '');
  const svgBlock = firstMatch(
    html,
    /<div\s+class="diagram-container"[^>]*>[\s\S]*?(<svg\b[\s\S]*?<\/svg>)[\s\S]*?<\/div>/i,
    'diagram SVG',
  );

  if ((svgBlock.match(/<svg\b/gi) || []).length !== 1) {
    fail('expected exactly one diagram SVG');
  }

  const darkVars = extractThemeVariables(
    css,
    /:root\s*,\s*\[data-theme="dark"\]\s*\{([\s\S]*?)\}/i,
    'dark',
  );
  const lightVars = extractThemeVariables(
    css,
    /\[data-theme="light"\]\s*\{([\s\S]*?)\}/i,
    'light',
  );

  const openTagMatch = svgBlock.match(/^<svg\b[^>]*>/i);
  if (!openTagMatch) fail('diagram SVG has no opening tag');
  let openTag = openTagMatch[0];
  openTag = ensureSvgAttribute(openTag, 'xmlns', 'http://www.w3.org/2000/svg');

  const viewBox = openTag.match(/\bviewBox="([^"]+)"/i)?.[1];
  if (!viewBox) fail('diagram SVG has no viewBox');
  const parts = viewBox.trim().split(/\s+/).map(Number);
  if (parts.length !== 4 || parts.some((value) => !Number.isFinite(value))) {
    fail(`invalid SVG viewBox: ${viewBox}`);
  }
  openTag = ensureSvgAttribute(openTag, 'width', String(parts[2]));
  openTag = ensureSvgAttribute(openTag, 'height', String(parts[3]));

  const inner = svgBlock.slice(openTagMatch[0].length, -'</svg>'.length);
  const fontFallback = [400, 500, 600, 700]
    .map(
      (weight) =>
        `@font-face { font-family: 'JetBrains Mono'; font-weight: ${weight}; src: local('JetBrains Mono'), local('JetBrainsMono-Regular'); }`,
    )
    .join('\n');

  const exportCss = `${fontFallback}\n${css}\n` +
    `:root, svg { ${darkVars} }\n` +
    `@media (prefers-color-scheme: light) { :root, svg { ${lightVars} } }\n` +
    `svg[data-theme="light"] { ${lightVars} }\n` +
    `svg[data-theme="dark"] { ${darkVars} }\n` +
    `rect.c-bg-rect { fill: var(--bg); }\n`;

  return `${openTag}\n<style><![CDATA[\n${exportCss}\n]]></style>\n` +
    `<rect width="100%" height="100%" class="c-bg-rect"/>\n${inner}\n</svg>\n`;
}

const [inputArg, outputArg] = process.argv.slice(2);
if (!inputArg || !outputArg) usage();

const input = path.resolve(inputArg);
const output = path.resolve(outputArg);
if (!fs.existsSync(input)) fail(`HTML input does not exist: ${input}`);

const html = fs.readFileSync(input, 'utf8');
const svg = standaloneSvg(html)
  .split('\n')
  .map((line) => line.replace(/[\t ]+$/g, ''))
  .join('\n');
if ((svg.match(/<svg\b/g) || []).length !== 1 || !svg.includes('viewBox=')) {
  fail('exported SVG failed structural validation');
}
if (/<(?:html|body|button)\b/i.test(svg) || /class="(?:toolbar|export-menu)"/.test(svg)) {
  fail('browser/UI markup leaked into SVG export');
}

fs.mkdirSync(path.dirname(output), { recursive: true });
fs.writeFileSync(output, svg, 'utf8');
console.log(output);
