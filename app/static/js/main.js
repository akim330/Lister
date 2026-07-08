(function () {
  const USER_ID_KEY = "lister.clientUuid";
  const USERNAME_KEY = "lister.username";

  function uuid() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      const r = Math.random() * 16 | 0;
      const v = c === "x" ? r : (r & 0x3 | 0x8);
      return v.toString(16);
    });
  }

  function getClientUuid() {
    let value = localStorage.getItem(USER_ID_KEY);
    if (!value) {
      value = uuid();
      localStorage.setItem(USER_ID_KEY, value);
    }
    return value;
  }

  function getUsername() {
    return localStorage.getItem(USERNAME_KEY) || "";
  }

  function setUsername(username) {
    localStorage.setItem(USERNAME_KEY, username || "Player");
  }

  async function saveUser(username) {
    const response = await fetch("/api/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ client_uuid: getClientUuid(), username })
    });
    if (!response.ok) throw new Error("Could not save user.");
    const data = await response.json();
    localStorage.setItem(USER_ID_KEY, data.client_uuid);
    setUsername(data.username);
    return data;
  }

  async function createGame(gameMode, categorySlug) {
    let username = getUsername();
    if (!username) {
      username = prompt("Choose a username:") || "Player";
      await saveUser(username);
    }

    const response = await fetch("/api/games", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        game_mode: gameMode,
        category_slug: categorySlug,
        client_uuid: getClientUuid(),
        username: getUsername() || username
      })
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || "Could not create game.");
    }
    return response.json();
  }

  window.ListerUser = {
    getClientUuid,
    getUsername,
    setUsername,
    saveUser,
    createGame
  };

  document.addEventListener("DOMContentLoaded", function () {
    const usernameInput = document.getElementById("usernameInput");
    const usernameStatus = document.getElementById("usernameStatus");
    const saveUsernameBtn = document.getElementById("saveUsernameBtn");
    const playSelectedBtn = document.getElementById("playSelectedBtn");
    const selectionStatus = document.getElementById("selectionStatus");
    const modeBtns = document.querySelectorAll(".mode-select-btn");
    const categorySelectBtns = document.querySelectorAll(".category-select-btn");
    const categoryBtns = document.querySelectorAll(".play-category-btn");
    // The home page now creates a game only after both choices are explicit.
    // Keeping the selected slugs in local state avoids hidden defaults and
    // makes the disabled Play button reflect exactly what will be submitted.
    let selectedMode = null;
    let selectedCategorySlug = null;
    let selectedCategoryName = null;

    if (usernameInput) {
      usernameInput.value = getUsername();
      if (getUsername() && usernameStatus) usernameStatus.textContent = `Playing as ${getUsername()}`;
    }

    if (saveUsernameBtn) {
      saveUsernameBtn.addEventListener("click", async function () {
        const username = (usernameInput.value || "").trim() || "Player";
        saveUsernameBtn.disabled = true;
        try {
          const data = await saveUser(username);
          if (usernameStatus) usernameStatus.textContent = `Playing as ${data.username}`;
        } catch (error) {
          if (usernameStatus) usernameStatus.textContent = error.message;
        } finally {
          saveUsernameBtn.disabled = false;
        }
      });
    }

    function updatePlayState() {
      if (!playSelectedBtn) return;
      const ready = Boolean(selectedMode && selectedCategorySlug);
      playSelectedBtn.disabled = !ready;
      if (!selectionStatus) return;
      if (ready) {
        selectionStatus.textContent = `Ready: ${selectedCategoryName}.`;
      } else if (selectedMode) {
        selectionStatus.textContent = "Choose a category to play.";
      } else if (selectedCategorySlug) {
        selectionStatus.textContent = "Choose a mode to play.";
      } else {
        selectionStatus.textContent = "Choose a mode and category to play.";
      }
    }

    modeBtns.forEach(function (btn) {
      btn.addEventListener("click", function () {
        selectedMode = btn.dataset.gameMode;
        modeBtns.forEach(function (otherBtn) {
          otherBtn.classList.toggle("selected", otherBtn === btn);
        });
        updatePlayState();
      });
    });

    categorySelectBtns.forEach(function (btn) {
      btn.addEventListener("click", function () {
        selectedCategorySlug = btn.dataset.categorySlug;
        selectedCategoryName = btn.dataset.categoryName || btn.textContent.trim();
        categorySelectBtns.forEach(function (otherBtn) {
          otherBtn.classList.toggle("selected", otherBtn === btn);
        });
        updatePlayState();
      });
    });

    if (playSelectedBtn) {
      playSelectedBtn.addEventListener("click", async function () {
        if (!selectedMode || !selectedCategorySlug) return;
        playSelectedBtn.disabled = true;
        try {
          const data = await createGame(selectedMode, selectedCategorySlug);
          window.location.href = data.play_url;
        } catch (error) {
          alert(error.message);
          playSelectedBtn.disabled = false;
          updatePlayState();
        }
      });
      updatePlayState();
    }

    categoryBtns.forEach(function (btn) {
      btn.addEventListener("click", async function () {
        btn.disabled = true;
        try {
          const data = await createGame(btn.dataset.gameMode || "survival", btn.dataset.categorySlug);
          window.location.href = data.play_url;
        } catch (error) {
          alert(error.message);
          btn.disabled = false;
        }
      });
    });
  });
})();
