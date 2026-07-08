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
  // Submitted answers wait here while the server verifies earlier answers.
  // Keeping a local FIFO queue lets the player keep typing without creating
  // concurrent writes against the same game session.
  const answerQueue = [];
  let isProcessingAnswerQueue = false;
  let answerInFlight = false;

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
    answerQueue.length = 0;
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

  async function applyAnswerResult(data) {
    // Every server response uses the same state update path that the old
    // single-answer submit handler used. Keeping this centralized prevents
    // queued answers from drifting away from the existing score, timer,
    // accepted-list, timeout, and completion behavior.
    if (data.status === "too_late") {
      answerQueue.length = 0;
      await finishGame("timeout");
      return;
    }
    if (data.game_ended && data.results_url) {
      gameEnded = true;
      answerQueue.length = 0;
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
  }

  function renderQueueProgress() {
    // The queue no longer disables typing, so the message area gives players a
    // lightweight signal that their submitted answers are still moving through
    // the ordered verification pipeline.
    const answerCount = answerQueue.length + (answerInFlight ? 1 : 0);
    if (gameEnded || answerCount === 0) return;
    setMessage(`Checking ${answerCount} ${answerCount === 1 ? "answer" : "answers"}...`, "");
  }

  async function processAnswerQueue() {
    // Only one worker may drain the queue. New submits can add more entries
    // while this loop is awaiting the API, and the loop will pick them up after
    // the current answer finishes.
    if (isProcessingAnswerQueue) return;
    isProcessingAnswerQueue = true;

    while (!gameEnded && answerQueue.length > 0) {
      // The server and database remain the source of truth for scoring and
      // timers, so requests are intentionally processed one at a time in the
      // exact order the player submitted them.
      const answer = answerQueue.shift();
      answerInFlight = true;
      renderQueueProgress();
      try {
        const data = await postJSON(`/api/games/${config.gameId}/answers`, { answer });
        await applyAnswerResult(data);
      } catch (error) {
        setMessage(error.message, "danger-text");
      } finally {
        answerInFlight = false;
      }
    }

    isProcessingAnswerQueue = false;
    if (!gameEnded) {
      renderQueueProgress();
      answerInput.disabled = false;
      answerInput.focus();
    }
  }

  function enqueueAnswer(answer) {
    // Submitting is now intentionally decoupled from verification: the typed
    // text is captured, the field clears immediately, and the queue worker owns
    // the eventual API call.
    answerQueue.push(answer);
    renderQueueProgress();
    processAnswerQueue();
  }

  answerForm.addEventListener("submit", function (event) {
    event.preventDefault();
    if (gameEnded) return;
    const answer = answerInput.value.trim();
    if (!answer) return;
    answerInput.value = "";
    answerInput.disabled = false;
    answerInput.focus();
    enqueueAnswer(answer);
  });

  stopBtn.addEventListener("click", function () {
    finishGame("stopped");
  });

  startGameAfterCountdown();
})();
