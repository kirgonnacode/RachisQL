const express = require("express");
const { createCanvas } = require("canvas");
const echarts = require("echarts");


echarts.setPlatformAPI({
  createCanvas() {
    return createCanvas();
  },
});

const app = express();
app.use(express.json({ limit: "2mb" }));

app.get("/health", (_req, res) => res.json({ status: "ok" }));

app.post("/render", (req, res) => {
  const { option, width = 800, height = 500 } = req.body || {};

  if (!option || typeof option !== "object" || Array.isArray(option)) {
    return res.status(400).json({ error: "Поле 'option' обязательно и должно быть объектом" });
  }

  const w = Number(width);
  const h = Number(height);
  if (!Number.isFinite(w) || !Number.isFinite(h) || w < 100 || w > 4000 || h < 100 || h > 4000) {
    return res.status(400).json({ error: "width/height должны быть числами в диапазоне 100-4000" });
  }

  let canvas;
  let chart;
  try {
    canvas = createCanvas(w, h);
    chart = echarts.init(canvas);
    chart.setOption(option);

    const buffer = canvas.toBuffer("image/png");
    res.set("Content-Type", "image/png");
    return res.send(buffer);
  } catch (err) {
    console.error("Ошибка рендера графика:", err);
    return res.status(500).json({ error: String(err && err.message ? err.message : err) });
  } finally {
    if (chart) chart.dispose();
  }
});

const PORT = process.env.PORT || 3000;
if (require.main === module) {
  app.listen(PORT, () => {
    console.log(`chart_renderer слушает порт ${PORT}`);
  });
}

module.exports = app;
