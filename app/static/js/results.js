(function () {
  document.addEventListener("DOMContentLoaded", function () {
    const card = document.querySelector(".results-card");
    if (!card) return;
    const gameId = card.dataset.gameId;
    const leaderboardMetric = card.dataset.leaderboardMetric || "score";
    const submitBtn = document.getElementById("submitScoreBtn");
    const doNotSubmitBtn = document.getElementById("doNotSubmitBtn");
    const submitPanel = document.getElementById("submitPanel");
    const leaderboardList = document.getElementById("leaderboardList");
    const generateChallengeLinkBtn = document.getElementById("generateChallengeLinkBtn");
    const challengeLinkInput = document.getElementById("challengeLinkInput");
    const challengeLinkStatus = document.getElementById("challengeLinkStatus");

    function formatTime(seconds) {
      seconds = Math.max(0, Math.floor(seconds || 0));
      const minutes = Math.floor(seconds / 60);
      const rest = seconds % 60;
      return `${minutes}:${String(rest).padStart(2, "0")}`;
    }

    function renderLeaderboard(entries) {
      leaderboardList.innerHTML = "";
      if (!entries.length) {
        const li = document.createElement("li");
        li.className = "muted";
        li.textContent = "No submitted scores yet.";
        leaderboardList.appendChild(li);
        return;
      }
      entries.forEach(function (entry) {
        const li = document.createElement("li");
        const value = leaderboardMetric === "elapsed" ? formatTime(entry.elapsed_seconds) : entry.score;
        // Leaderboard rows link to the completed session so friends can inspect
        // the database-backed list behind the submitted score, not just the
        // numeric ranking.
        li.innerHTML = `<span class="rank">${entry.rank}.</span> <strong></strong> — ${value} <a class="inline-link"></a>`;
        li.querySelector("strong").textContent = entry.username;
        const link = li.querySelector("a");
        link.href = entry.results_url || `/results/${entry.game_session_id}`;
        link.textContent = "View list";
        leaderboardList.appendChild(li);
      });
    }

    async function copyChallengeLink(link) {
      // Clipboard access is available only in secure browser contexts. Keeping
      // the generated link visible means the feature still works locally or in
      // older browsers even when automatic copy is denied.
      if (!navigator.clipboard || !window.isSecureContext) return false;
      try {
        await navigator.clipboard.writeText(link);
        return true;
      } catch (error) {
        return false;
      }
    }

    if (generateChallengeLinkBtn && challengeLinkInput) {
      generateChallengeLinkBtn.addEventListener("click", async function () {
        const challengeUrl = new URL(`/challenge/${encodeURIComponent(gameId)}`, window.location.origin).toString();
        challengeLinkInput.value = challengeUrl;
        challengeLinkInput.classList.remove("hidden");
        challengeLinkInput.focus();
        challengeLinkInput.select();

        const copied = await copyChallengeLink(challengeUrl);
        if (challengeLinkStatus) {
          challengeLinkStatus.textContent = copied ? "Challenge link copied." : "Challenge link generated.";
        }
      });
    }

    if (submitBtn) {
      submitBtn.addEventListener("click", async function () {
        submitBtn.disabled = true;
        try {
          const response = await fetch(`/api/games/${gameId}/submit-score`, { method: "POST" });
          const data = await response.json();
          if (!response.ok) throw new Error(data.error || "Could not submit score.");
          renderLeaderboard(data.leaderboard || []);
          if (submitPanel) submitPanel.innerHTML = '<p class="success">Score submitted.</p>';
        } catch (error) {
          alert(error.message);
          submitBtn.disabled = false;
        }
      });
    }

    if (doNotSubmitBtn) {
      doNotSubmitBtn.addEventListener("click", function () {
        if (submitPanel) submitPanel.innerHTML = '<p class="muted">Score kept private.</p>';
      });
    }
  });
})();
