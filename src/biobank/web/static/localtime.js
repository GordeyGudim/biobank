/* Время журнала — в часовом поясе браузера.
 *
 * Сервер пишет время в UTC: <time class="utc" datetime="2026-10-20T08:24:14Z">20.10.2026 08:24 UTC</time>.
 * Скрипт заменяет текст на местное время с подписью пояса: «20.10.2026 15:24 (UTC+7)».
 * Без JavaScript остаётся UTC с подписью — тоже правильно, только не в местном поясе.
 * Свой JavaScript здесь, потому что пояс пользователя знает только браузер, а не сервер.
 */
(function () {
  function two(n) { return (n < 10 ? "0" : "") + n; }

  function zone(date) {
    var minutes = -date.getTimezoneOffset();  // getTimezoneOffset: UTC минус местное, в минутах
    var sign = minutes < 0 ? "-" : "+";
    minutes = Math.abs(minutes);
    var text = "UTC" + sign + Math.floor(minutes / 60);
    if (minutes % 60) { text += ":" + two(minutes % 60); }
    return minutes === 0 ? "UTC" : text;
  }

  function convert(root) {
    var items = (root || document).querySelectorAll("time.utc");
    for (var i = 0; i < items.length; i++) {
      var date = new Date(items[i].getAttribute("datetime"));
      if (isNaN(date.getTime())) { continue; }
      items[i].textContent = two(date.getDate()) + "." + two(date.getMonth() + 1) + "." +
        date.getFullYear() + " " + two(date.getHours()) + ":" + two(date.getMinutes()) +
        " (" + zone(date) + ")";
      items[i].classList.remove("utc");  // уже переведено — второй раз не трогать
      items[i].title = "в поясе вашего компьютера";
    }
  }

  document.addEventListener("DOMContentLoaded", function () { convert(document); });
  // HTMX вставил новый кусок страницы (таблицу журнала, карточку) — перевести и в нём
  document.addEventListener("htmx:load", function (event) { convert(event.target); });
})();
