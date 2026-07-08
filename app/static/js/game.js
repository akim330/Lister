(function () {
  const config = window.LISTER_GAME;
  if (!config) return;

  // Countdown modes render and mutate `remaining`, while target-list modes
  // render and mutate `elapsed`. Keeping both counters separate makes answer
  // responses from the server easy to apply without converting between timer
  // meanings.
  let remaining = config.startingSeconds || 0;
  let elapsed = 0;
  let intervalId = null;
  let gameEnded = false;
  let answerPending = false;

  const countdownEl = document.getElementById("countdown");
  const gameArea = document.getElementById("gameArea");
  const timerLabelEl = document.getElementById("timerLabel");
  const timerEl = document.getElementById("timer");
  const scoreEl = document.getElementById("score");
  const answerForm = document.getElementById("answerForm");
  const answerInput = document.getElementById("answerInput");
  const messageEl = document.getElementById("message");
  const acceptedAnswersEl = document.getElementById("acceptedAnswers");
  const stopBtn = document.getElementById("stopBtn");

  function formatTime(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    const minutes = Math.floor(seconds / 60);
    const rest = seconds % 60;
    return `${minutes}:${String(rest).padStart(2, "0")}`;
  }

  function setMessage(text, kind) {
    messageEl.textContent = text || "";
    messageEl.className = `message ${kind || ""}`;
  }

  function renderTimer() {
    if (config.timerKind === "countup") {
      timerEl.textContent = formatTime(elapsed);
    } else {
      timerEl.textContent = formatTime(remaining);
    }
  }

  function renderAnswers(answers) {
    acceptedAnswersEl.innerHTML = "";
    if (!answers || !answers.length) return;
    answers.forEach(function (answer) {
      const li = document.createElement("li");
      li.className = answer.status;
      if (answer.status === "replaced") {
        const oldName = document.createElement("span");
        oldName.textContent = answer.name;
        const note = document.createElement("span");
        note.textContent = ` — replaced by ${answer.replaced_by}`;
        li.appendChild(oldName);
        li.appendChild(note);
      } else {
        li.textContent = answer.name;
      }
      acceptedAnswersEl.appendChild(li);
    });
  }

  async function postJSON(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
    });
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) {
      throw new Error("Request failed. Please try again.");
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || data.message || "Request failed.");
    return data;
  }

  async function finishGame(reason) {
    if (gameEnded) return;
    gameEnded = true;
    clearInterval(intervalId);
    answerInput.disabled = true;
    stopBtn.disabled = true;
    setMessage("Ending game...", "");
    const data = await postJSON(`/api/games/${config.gameId}/end`, { reason });
    window.location.href = data.results_url;
  }

  function beginTimer() {
    if (timerLabelEl) timerLabelEl.textContent = config.timerKind === "countup" ? "Elapsed" : "Time";
    renderTimer();
    intervalId = setInterval(function () {
      // The client timer is only a display aid. The server remains the source
      // of truth after every answer and when countdown modes expire.
      if (config.timerKind === "countup") {
        elapsed += 1;
      } else {
        remaining -= 1;
      }
      renderTimer();
      if (config.timerKind !== "countup" && remaining <= 0) finishGame("timeout");
    }, 1000);
  }

  async function startGameAfterCountdown() {
    const ticks = ["3", "2", "1", "Start"];
    let index = 0;
    countdownEl.textContent = ticks[index];

    const countdownInterval = setInterval(async function () {
      index += 1;
      if (index < ticks.length) {
        countdownEl.textContent = ticks[index];
        return;
      }
      clearInterval(countdownInterval);
      countdownEl.classList.add("hidden");
      gameArea.classList.remove("hidden");
      try {
        const data = await postJSON(`/api/games/${config.gameId}/start`, {});
        if (typeof data.remaining_seconds === "number") remaining = data.remaining_seconds;
        if (typeof data.elapsed_seconds === "number") elapsed = data.elapsed_seconds;
        scoreEl.textContent = data.score;
        beginTimer();
        answerInput.focus();
      } catch (error) {
        setMessage(error.message, "danger-text");
      }
    }, 800);
  }

  answerForm.addEventListener("submit", async function (event) {
    event.preventDefault();
    if (gameEnded || answerPending) return;
    const answer = answerInput.value.trim();
    if (!answer) return;
    answerInput.value = "";
    answerPending = true;
    answerInput.disabled = true;

    try {
      const data = await postJSON(`/api/games/${config.gameId}/answers`, { answer });
      if (data.status === "too_late") {
        await finishGame("timeout");
        return;
      }
      if (data.game_ended && data.results_url) {
        gameEnded = true;
        clearInterval(intervalId);
        window.location.href = data.results_url;
        return;
      }
      if (typeof data.current_score === "number") scoreEl.textContent = data.current_score;
      if (typeof data.remaining_seconds === "number") remaining = data.remaining_seconds;
      if (typeof data.elapsed_seconds === "number") elapsed = data.elapsed_seconds;
      renderTimer();
      renderAnswers(data.accepted_answers || []);
      const good = ["accepted", "replaced", "completed"].includes(data.status);
      setMessage(data.message, good ? "success" : "");
    } catch (error) {
      setMessage(error.message, "danger-text");
    } finally {
      answerPending = false;
      if (!gameEnded) {
        answerInput.disabled = false;
        answerInput.focus();
      }
    }
  });

  stopBtn.addEventListener("click", function () {
    finishGame("stopped");
  });

  startGameAfterCountdown();
})();
