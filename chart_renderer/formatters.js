const MARKER_PREFIX = "__fmt:";
const MAX_DEPTH = 50;

const ruNumber = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
const ruSmallNumber = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 4 });

function formatNumber(value) {
  if (value === null || value === undefined) {
    return "";
  }
  if (typeof value !== "number") {
    return String(value);
  }
  if (!Number.isFinite(value)) {
    return "—";
  }
  const text = (Math.abs(value) < 1 ? ruSmallNumber : ruNumber).format(value);
  return text === "-0" ? "0" : text;
}

function extractValue(arg) {
  if (arg !== null && typeof arg === "object") {
    const v = arg.value;
    return Array.isArray(v) ? v[v.length - 1] : v;
  }
  return arg;
}

const FORMATTERS = {
  number: (arg) => formatNumber(extractValue(arg)),
  pie_label: (params) => `${params.name}\n${ruNumber.format(params.percent)}%`,
};

function applyFormatters(node, depth = 0) {
  if (depth > MAX_DEPTH || node === null || typeof node !== "object") {
    return node;
  }
  if (Array.isArray(node)) {
    node.forEach((item) => applyFormatters(item, depth + 1));
    return node;
  }
  for (const [key, value] of Object.entries(node)) {
    if (key === "formatter" && typeof value === "string" && value.startsWith(MARKER_PREFIX)) {
      const fn = FORMATTERS[value.slice(MARKER_PREFIX.length)];
      if (fn) {
        node[key] = fn;
      } else {
        console.warn(`Неизвестный форматтер ${value}, будет применен формат ECharts по умолчанию`);
        delete node[key];
      }
    } else {
      applyFormatters(value, depth + 1);
    }
  }
  return node;
}

module.exports = { applyFormatters, formatNumber };