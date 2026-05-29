document.addEventListener("DOMContentLoaded", () => {
  // 外部画像の読み込みに失敗した場合、カード全体を崩さないように隠します。
  document.querySelectorAll("img").forEach((image) => {
    image.addEventListener("error", () => {
      image.style.display = "none";
    });
  });
});
