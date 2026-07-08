(function () {
  document.addEventListener("DOMContentLoaded", async function () {
    const container = document.getElementById("scoresContainer");
    const clientUuid = window.ListerUser ? window.ListerUser.getClientUuid() : localStorage.getItem("lister.clientUuid");
    if (!clientUuid) {
      container.innerHTML = '<p class="muted">No scores for this browser yet.</p>';
      return;
    }

    try {
      const response = await fetch(`/api/me/scores?client_uuid=${encodeURIComponent(clientUuid)}`);
      const data = await response.json();
      const scores = data.scores || [];
      if (!scores.length) {
        container.innerHTML = '<p class="muted">No scores for this browser yet.</p>';
        return;
      }
      const rows = scores.map(function (score) {
        const date = score.ended_at ? new Date(score.ended_at).toLocaleString() : "Unknown";
        const elapsed = typeof score.elapsed_seconds === "number" ? formatTime(score.elapsed_seconds) : "";
        return `<tr>
          <td>${escapeHtml(score.mode_name)}</td>
          <td>${escapeHtml(score.category_name)}</td>
          <td>${score.score}</td>
          <td>${escapeHtml(elapsed)}</td>
          <td>${score.submitted ? "Submitted" : "Private"}</td>
          <td>${score.end_reason || "ended"}</td>
          <td>${date}</td>
          <td><a class="inline-link" href="${escapeHtml(score.results_url)}">View list</a></td>
        </tr>`;
      }).join("");
      container.innerHTML = `<table>
        <thead><tr><th>Mode</th><th>Category</th><th>Score</th><th>Elapsed</th><th>Status</th><th>Ended By</th><th>Date</th><th>List</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    } catch (error) {
      container.innerHTML = `<p>${escapeHtml(error.message)}</p>`;
    }
  });

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function formatTime(seconds) {
    seconds = Math.max(0, Math.floor(seconds || 0));
    const minutes = Math.floor(seconds / 60);
    const rest = seconds % 60;
    return `${minutes}:${String(rest).padStart(2, "0")}`;
  }
})();
