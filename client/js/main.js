/* Walking skeleton: no input, prediction, or gameplay. */
const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
socket.addEventListener("message", ({ data }) => {
  try {
    const message = JSON.parse(data);
    if (message?.t === "welcome") console.log("Terra welcome", message);
  } catch (error) {
    console.error("Terra malformed frame", error);
  }
});
socket.addEventListener("close", () => console.log("Terra disconnected"));
socket.addEventListener("error", (error) => console.error("Terra connection error", error));

new Phaser.Game({
  type: Phaser.AUTO,
  width: 800,
  height: 600,
  backgroundColor: "#24382c",
  scene: {
    create() {
      const grid = this.add.graphics({ lineStyle: { width: 1, color: 0x45634c, alpha: 0.5 } });
      for (let x = 0; x <= 800; x += 40) grid.lineBetween(x, 0, x, 600);
      for (let y = 0; y <= 600; y += 40) grid.lineBetween(0, y, 800, y);
    },
  },
});
