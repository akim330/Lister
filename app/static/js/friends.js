(function () {
  document.addEventListener("DOMContentLoaded", function () {
    const elements = {
      userStatus: document.getElementById("friendsUserStatus"),
      requestForm: document.getElementById("friendRequestForm"),
      requestInput: document.getElementById("friendUsernameInput"),
      requestStatus: document.getElementById("friendRequestStatus"),
      incomingRequests: document.getElementById("incomingRequests"),
      outgoingRequests: document.getElementById("outgoingRequests"),
      friendsList: document.getElementById("friendsList"),
      friendModeBtn: document.getElementById("friendModeBtn"),
      categoryModeBtn: document.getElementById("categoryModeBtn"),
      friendModePanel: document.getElementById("friendModePanel"),
      categoryModePanel: document.getElementById("categoryModePanel"),
      friendSelect: document.getElementById("friendSelect"),
      categorySelect: document.getElementById("categorySelect"),
      friendResults: document.getElementById("friendComparisonResults"),
      categoryResults: document.getElementById("categoryComparisonResults")
    };

    // The Friends page is fully driven by API state. Keeping the latest
    // overview payload here lets request actions refresh the side panel and
    // select boxes without asking the server for comparison data until the
    // player chooses a specific friend or category.
    const state = {
      overview: null,
      currentMode: "friend"
    };

    initialize();

    async function initialize() {
      wireEvents();
      try {
        await ensureNamedUser();
        await loadOverview();
      } catch (error) {
        showStatus(elements.userStatus, error.message, true);
      }
    }

    function wireEvents() {
      elements.requestForm.addEventListener("submit", async function (event) {
        event.preventDefault();
        await sendFriendRequest();
      });
      elements.friendModeBtn.addEventListener("click", function () {
        switchMode("friend");
      });
      elements.categoryModeBtn.addEventListener("click", function () {
        switchMode("category");
      });
      elements.friendSelect.addEventListener("change", loadSelectedFriendComparison);
      elements.categorySelect.addEventListener("change", loadSelectedCategoryComparison);
    }

    async function ensureNamedUser() {
      if (!window.ListerUser) throw new Error("Could not load local user tools.");
      let username = window.ListerUser.getUsername();
      if (!username) {
        username = prompt("Choose a username for friend requests:") || "Player";
      }
      const data = await window.ListerUser.saveUser(username);
      showStatus(elements.userStatus, `Playing as ${data.username}.`);
    }

    function identityParams() {
      // Every Friends endpoint receives the browser-local identity explicitly
      // because this app does not have account sessions yet.
      const params = new URLSearchParams();
      params.set("client_uuid", window.ListerUser.getClientUuid());
      params.set("username", window.ListerUser.getUsername() || "Player");
      return params;
    }

    function identityPayload(extra) {
      return Object.assign({
        client_uuid: window.ListerUser.getClientUuid(),
        username: window.ListerUser.getUsername() || "Player"
      }, extra || {});
    }

    async function loadOverview() {
      const data = await fetchJson(`/api/friends?${identityParams().toString()}`);
      state.overview = data;
      renderOverview();
      if (state.currentMode === "friend") {
        await loadSelectedFriendComparison();
      } else {
        await loadSelectedCategoryComparison();
      }
    }

    function renderOverview() {
      showStatus(elements.userStatus, `Playing as ${state.overview.user.username}.`);
      renderIncomingRequests(state.overview.incoming_requests || []);
      renderOutgoingRequests(state.overview.outgoing_requests || []);
      renderFriends(state.overview.friends || []);
      renderFriendOptions(state.overview.friends || []);
      renderCategoryOptions(state.overview.categories || []);
    }

    function renderIncomingRequests(requests) {
      if (!requests.length) {
        elements.incomingRequests.innerHTML = '<p class="muted">No incoming requests.</p>';
        return;
      }
      elements.incomingRequests.innerHTML = requests.map(function (request) {
        return `<div class="friend-list-item">
          <strong>${escapeHtml(request.username)}</strong>
          <div class="friend-list-actions">
            <button class="secondary" type="button" data-accept-request="${request.id}">Accept</button>
            <button class="danger" type="button" data-decline-request="${request.id}">Decline</button>
          </div>
        </div>`;
      }).join("");
      elements.incomingRequests.querySelectorAll("[data-accept-request]").forEach(function (button) {
        button.addEventListener("click", async function () {
          await answerFriendRequest(button.dataset.acceptRequest, "accept");
        });
      });
      elements.incomingRequests.querySelectorAll("[data-decline-request]").forEach(function (button) {
        button.addEventListener("click", async function () {
          await answerFriendRequest(button.dataset.declineRequest, "decline");
        });
      });
    }

    function renderOutgoingRequests(requests) {
      if (!requests.length) {
        elements.outgoingRequests.innerHTML = '<p class="muted">No sent requests.</p>';
        return;
      }
      elements.outgoingRequests.innerHTML = requests.map(function (request) {
        return `<div class="friend-list-item"><strong>${escapeHtml(request.username)}</strong><span class="muted">Pending</span></div>`;
      }).join("");
    }

    function renderFriends(friends) {
      if (!friends.length) {
        elements.friendsList.innerHTML = '<p class="muted">No friends yet.</p>';
        return;
      }
      elements.friendsList.innerHTML = friends.map(function (friend) {
        return `<button class="friend-pill" type="button" data-friend-id="${friend.id}">${escapeHtml(friend.username)}</button>`;
      }).join("");
      elements.friendsList.querySelectorAll("[data-friend-id]").forEach(function (button) {
        button.addEventListener("click", function () {
          switchMode("friend");
          elements.friendSelect.value = button.dataset.friendId;
          loadSelectedFriendComparison();
        });
      });
    }

    function renderFriendOptions(friends) {
      if (!friends.length) {
        elements.friendSelect.innerHTML = '<option value="">No friends yet</option>';
        elements.friendSelect.disabled = true;
        return;
      }
      elements.friendSelect.disabled = false;
      elements.friendSelect.innerHTML = friends.map(function (friend) {
        return `<option value="${friend.id}">${escapeHtml(friend.username)}</option>`;
      }).join("");
    }

    function renderCategoryOptions(categories) {
      if (!categories.length) {
        elements.categorySelect.innerHTML = '<option value="">No completed categories</option>';
        elements.categorySelect.disabled = true;
        return;
      }
      elements.categorySelect.disabled = false;
      elements.categorySelect.innerHTML = categories.map(function (category) {
        return `<option value="${category.id}">${escapeHtml(category.name)}</option>`;
      }).join("");
    }

    async function sendFriendRequest() {
      const targetUsername = (elements.requestInput.value || "").trim();
      if (!targetUsername) {
        showStatus(elements.requestStatus, "Enter an exact username.", true);
        return;
      }
      elements.requestForm.querySelector("button").disabled = true;
      try {
        const data = await fetchJson("/api/friends/requests", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(identityPayload({ target_username: targetUsername }))
        });
        elements.requestInput.value = "";
        showStatus(elements.requestStatus, data.message || "Friend request sent.");
        await loadOverview();
      } catch (error) {
        showStatus(elements.requestStatus, error.message, true);
      } finally {
        elements.requestForm.querySelector("button").disabled = false;
      }
    }

    async function answerFriendRequest(requestId, action) {
      try {
        await fetchJson(`/api/friends/requests/${encodeURIComponent(requestId)}/${action}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(identityPayload())
        });
        await loadOverview();
      } catch (error) {
        showStatus(elements.requestStatus, error.message, true);
      }
    }

    function switchMode(mode) {
      state.currentMode = mode;
      const friendMode = mode === "friend";
      elements.friendModeBtn.classList.toggle("selected", friendMode);
      elements.categoryModeBtn.classList.toggle("selected", !friendMode);
      elements.friendModePanel.classList.toggle("hidden", !friendMode);
      elements.categoryModePanel.classList.toggle("hidden", friendMode);
      if (friendMode) {
        loadSelectedFriendComparison();
      } else {
        loadSelectedCategoryComparison();
      }
    }

    async function loadSelectedFriendComparison() {
      const friendId = elements.friendSelect.value;
      if (!friendId) {
        elements.friendResults.innerHTML = '<p class="muted">Add a friend to compare shared lists.</p>';
        return;
      }
      elements.friendResults.innerHTML = '<p class="muted">Loading comparison...</p>';
      try {
        const data = await fetchJson(`/api/friends/${encodeURIComponent(friendId)}/comparison?${identityParams().toString()}`);
        renderFriendComparison(data);
      } catch (error) {
        elements.friendResults.innerHTML = `<p>${escapeHtml(error.message)}</p>`;
      }
    }

    async function loadSelectedCategoryComparison() {
      const categoryId = elements.categorySelect.value;
      if (!categoryId) {
        elements.categoryResults.innerHTML = '<p class="muted">Complete a category to compare it with friends.</p>';
        return;
      }
      elements.categoryResults.innerHTML = '<p class="muted">Loading comparison...</p>';
      try {
        const data = await fetchJson(`/api/friends/categories/${encodeURIComponent(categoryId)}/comparison?${identityParams().toString()}`);
        renderCategoryComparison(data);
      } catch (error) {
        elements.categoryResults.innerHTML = `<p>${escapeHtml(error.message)}</p>`;
      }
    }

    function renderFriendComparison(data) {
      const categories = data.categories || [];
      if (!categories.length) {
        elements.friendResults.innerHTML = `<p class="muted">No shared completed categories with ${escapeHtml(data.friend.username)} yet.</p>`;
        return;
      }
      elements.friendResults.innerHTML = categories.map(function (category) {
        const modeNote = category.you.game_mode !== category.friend.game_mode
          ? `<p class="muted">Different modes: ${escapeHtml(category.you.mode_name)} and ${escapeHtml(category.friend.mode_name)}.</p>`
          : "";
        return `<article class="comparison-card">
          <h2>${escapeHtml(category.category_name)}</h2>
          ${modeNote}
          <div class="comparison-columns">
            ${renderRunColumn(category.you)}
            ${renderRunColumn(category.friend)}
          </div>
        </article>`;
      }).join("");
    }

    function renderCategoryComparison(data) {
      const runs = data.runs || [];
      if (!runs.length) {
        elements.categoryResults.innerHTML = '<p class="muted">No completed runs found for this category.</p>';
        return;
      }
      elements.categoryResults.innerHTML = `<article class="comparison-card">
        <h2>${escapeHtml(data.category.name)}</h2>
        <div class="comparison-columns multi">
          ${runs.map(renderRunColumn).join("")}
        </div>
      </article>`;
    }

    function renderRunColumn(run) {
      const elapsed = typeof run.elapsed_seconds === "number" ? formatTime(run.elapsed_seconds) : "Not timed";
      const answers = (run.answers || []).map(function (answer) {
        return `<li>${escapeHtml(answer.name)}</li>`;
      }).join("") || '<li class="muted">No accepted answers saved.</li>';
      return `<div class="run-column">
        <div class="run-column-header">
          <strong>${escapeHtml(run.username)}</strong>
          <span class="muted">${escapeHtml(run.mode_name)}</span>
        </div>
        <p class="score-line">Score ${run.score} · ${escapeHtml(elapsed)} · <a class="inline-link" href="${escapeHtml(run.results_url)}">View list</a></p>
        <ol class="answer-list">${answers}</ol>
      </div>`;
    }

    async function fetchJson(url, options) {
      const response = await fetch(url, options || {});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Something went wrong.");
      return data;
    }

    function showStatus(element, message, isError) {
      element.textContent = message || "";
      element.classList.toggle("error", Boolean(isError));
    }

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
  });
})();
