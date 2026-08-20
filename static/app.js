/**
 * Provider NPI & Credential Verification Suite - Client Application
 */

document.addEventListener("DOMContentLoaded", () => {
  // Navigation Tabs
  const tabBtns = document.querySelectorAll(".tab-btn");
  const tabContents = document.querySelectorAll(".tab-content");

  tabBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      const targetTab = btn.getAttribute("data-tab");

      tabBtns.forEach(b => b.classList.remove("active"));
      tabContents.forEach(c => c.classList.add("hidden"));

      btn.classList.add("active");
      document.getElementById(targetTab).classList.remove("hidden");
    });
  });

  // Global State
  let currentSessionId = null;
  let currentOriginalFilename = "provider_output";
  let eventSource = null;
  let processedRows = [];
  let totalRowsCount = 0;
  let verifiedCount = 0;
  let credFoundCount = 0;
  let invalidNpiCount = 0;

  // DOM Elements
  const dropZone = document.getElementById("drop-zone");
  const fileInput = document.getElementById("file-input");
  const loadSampleBtn = document.getElementById("load-sample-btn");
  const fileSummary = document.getElementById("file-summary");
  const fileNameText = document.getElementById("file-name-text");
  const fileRowsText = document.getElementById("file-rows-text");
  const startVerifyBtn = document.getElementById("start-verify-btn");

  const progressCard = document.getElementById("progress-card");
  const progressFill = document.getElementById("progress-fill");
  const progressPercentage = document.getElementById("progress-percentage");
  const progressCounts = document.getElementById("progress-counts");
  const progressStatusBadge = document.getElementById("progress-status-badge");
  const logConsole = document.getElementById("log-console");

  const statTotal = document.getElementById("stat-total");
  const statVerified = document.getElementById("stat-verified");
  const statCredFound = document.getElementById("stat-cred-found");
  const statInvalidNpi = document.getElementById("stat-invalid-npi");

  const resultsCard = document.getElementById("results-card");
  const tableBody = document.getElementById("table-body");
  const tableSearchInput = document.getElementById("table-search-input");
  const tableFilterSelect = document.getElementById("table-filter-select");

  const downloadXlsxBtn = document.getElementById("download-xlsx-btn");
  const downloadCsvBtn = document.getElementById("download-csv-btn");
  const downloadJsonBtn = document.getElementById("download-json-btn");

  // Single Lookup Sandbox
  const singleForm = document.getElementById("single-lookup-form");
  const spSubmitBtn = document.getElementById("sp-submit-btn");
  const spResultContainer = document.getElementById("sp-result-container");
  const spResultContent = document.getElementById("sp-result-content");

  // Drag & Drop Handlers
  dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("drag-over");
  });

  dropZone.addEventListener("dragleave", () => {
    dropZone.classList.remove("drag-over");
  });

  dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("drag-over");

    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      handleFileUpload(e.dataTransfer.files[0]);
    }
  });

  fileInput.addEventListener("change", (e) => {
    if (e.target.files && e.target.files.length > 0) {
      handleFileUpload(e.target.files[0]);
    }
  });

  loadSampleBtn.addEventListener("click", () => {
    appendLog("info", "[System] Requesting sample dataset load...");
    fetch("/api/sample")
      .then(res => res.json())
      .then(data => {
        if (data.error) {
          alert("Error loading sample: " + data.error);
          return;
        }
        setupSessionUI(data);
        appendLog("success", `[System] Loaded sample dataset (${data.row_count} rows).`);
      })
      .catch(err => alert("Error: " + err));
  });

  function handleFileUpload(file) {
    const formData = new FormData();
    formData.append("file", file);

    appendLog("info", `[Upload] Uploading ${file.name}...`);

    fetch("/api/upload", {
      method: "POST",
      body: formData
    })
      .then(res => res.json())
      .then(data => {
        if (data.error) {
          alert("Upload failed: " + data.error);
          return;
        }
        setupSessionUI(data);
        appendLog("success", `[Upload] Successfully parsed ${data.filename} (${data.row_count} rows).`);
      })
      .catch(err => alert("Upload error: " + err));
  }

  function setupSessionUI(data) {
    currentSessionId = data.session_id;
    if (data.filename) {
      const dotIndex = data.filename.lastIndexOf('.');
      currentOriginalFilename = dotIndex !== -1 ? data.filename.substring(0, dotIndex) : data.filename;
    } else {
      currentOriginalFilename = "provider_output";
    }
    totalRowsCount = data.row_count;
    fileNameText.textContent = data.filename;
    fileRowsText.textContent = `${data.row_count} provider rows ready`;

    fileSummary.classList.remove("hidden");
    resultsCard.classList.add("hidden");
    progressCard.classList.add("hidden");

    // Reset stats
    processedRows = [];
    verifiedCount = 0;
    credFoundCount = 0;
    invalidNpiCount = 0;

    statTotal.textContent = totalRowsCount;
    statVerified.textContent = 0;
    statCredFound.textContent = 0;
    statInvalidNpi.textContent = 0;
  }

  // Start Verification Stream
  startVerifyBtn.addEventListener("click", () => {
    if (!currentSessionId) return;

    // Reset UI
    progressCard.classList.remove("hidden");
    resultsCard.classList.remove("hidden");
    tableBody.innerHTML = "";
    logConsole.innerHTML = "";

    progressFill.style.width = "0%";
    progressPercentage.textContent = "0%";
    progressCounts.textContent = `0 / ${totalRowsCount} Processed`;
    progressStatusBadge.textContent = "Processing Batch...";
    progressStatusBadge.className = "badge badge-processing";

    appendLog("info", "[Pipeline] Initializing NPPES Registry & Web Scraper stream...");

    if (eventSource) eventSource.close();

    eventSource = new EventSource(`/api/process_stream/${currentSessionId}`);

    eventSource.onmessage = (event) => {
      const msg = JSON.parse(event.data);

      if (msg.type === "progress") {
        updateProgress(msg);
      } else if (msg.type === "finished") {
        eventSource.close();
        progressFill.style.width = "100%";
        progressPercentage.textContent = "100%";
        progressStatusBadge.textContent = "Verification Complete";
        progressStatusBadge.className = "badge badge-success";
        appendLog("success", `[Pipeline] Batch verification completed successfully! Invalid/Mismatched NPIs: ${msg.invalid_count + msg.mismatch_count}`);
      } else if (msg.type === "error") {
        eventSource.close();
        progressStatusBadge.textContent = "Error Occurred";
        progressStatusBadge.className = "badge badge-danger";
        appendLog("error", `[Pipeline Error] ${msg.message}`);
      }
    };

    eventSource.onerror = (err) => {
      eventSource.close();
      appendLog("error", "[Connection Error] SSE stream closed unexpectedly.");
    };
  });

  function updateProgress(msg) {
    const row = msg.row_summary;
    processedRows.push(row);

    // Update Progress Bar
    progressFill.style.width = `${msg.percent}%`;
    progressPercentage.textContent = `${msg.percent}%`;
    progressCounts.textContent = `${msg.current} / ${msg.total} Processed`;

    // Update Stats
    if (row.status === "Verified") verifiedCount++;
    if (row.credential && row.credential !== "Unable to verify") credFoundCount++;
    if (row.is_invalid_npi || row.is_mismatch_npi) invalidNpiCount++;

    statVerified.textContent = verifiedCount;
    statCredFound.textContent = credFoundCount;
    statInvalidNpi.textContent = invalidNpiCount;

    // Log Activity
    const alertTag = (row.is_invalid_npi || row.is_mismatch_npi) ? " [ALERT: NPI Issue]" : "";
    appendLog(
      row.is_invalid_npi || row.is_mismatch_npi ? "warn" : "info",
      `[Row ${row.row_index + 1}] ${row.provider_name} -> NPI1: ${row.npi1} | Cred: ${row.credential}${alertTag}`
    );

    // Append to Table
    renderTableRow(row);
  }

  function renderTableRow(row) {
    const tr = document.createElement("tr");
    if (row.is_invalid_npi || row.is_mismatch_npi) {
      tr.classList.add("row-invalid");
    }

    const npiDisplay = row.npi ? row.npi : "<span class='text-dim'>Blank</span>";
    const npi1Display = row.npi1 !== "Not Found" ? `<strong>${row.npi1}</strong>` : "<span class='badge badge-danger'>Not Found</span>";
    const credDisplay = (row.credential && row.credential !== "Unable to verify")
      ? `<span class='badge badge-success'>${row.credential}</span>`
      : "<span class='badge badge-warning'>Unable to verify</span>";

    const statusDisplay = row.status === "Verified"
      ? "<span class='badge badge-success'>Verified</span>"
      : "<span class='badge badge-danger'>Not Verified</span>";

    const npi1UrlLink = row.npi1_url
      ? `<a href="${row.npi1_url}" target="_blank" class="link-badge">NPPES Link</a>`
      : "-";

    const webUrlLink = row.url
      ? `<a href="${row.url}" target="_blank" class="link-badge">Profile Page</a>`
      : "-";

    tr.innerHTML = `
      <td>${row.row_index + 1}</td>
      <td><strong>${escapeHtml(row.provider_name)}</strong></td>
      <td>${npiDisplay}</td>
      <td>${npi1Display}</td>
      <td>${credDisplay}</td>
      <td>${statusDisplay}</td>
      <td>${npi1UrlLink}</td>
      <td>${webUrlLink}</td>
    `;

    tableBody.appendChild(tr);
  }

  // Filter & Search Controls
  tableSearchInput.addEventListener("input", filterTableRows);
  tableFilterSelect.addEventListener("change", filterTableRows);

  function filterTableRows() {
    const query = tableSearchInput.value.toLowerCase();
    const filter = tableFilterSelect.value;

    const trs = tableBody.querySelectorAll("tr");
    trs.forEach((tr, idx) => {
      const rowData = processedRows[idx];
      if (!rowData) return;

      let matchesSearch = true;
      if (query) {
        const text = tr.textContent.toLowerCase();
        matchesSearch = text.includes(query);
      }

      let matchesFilter = true;
      if (filter === "verified") {
        matchesFilter = rowData.status === "Verified";
      } else if (filter === "invalid") {
        matchesFilter = rowData.is_invalid_npi || rowData.is_mismatch_npi;
      } else if (filter === "cred_verified") {
        matchesFilter = rowData.credential && rowData.credential !== "Unable to verify";
      }

      tr.style.display = (matchesSearch && matchesFilter) ? "" : "none";
    });
  }

  // Helper for downloads with explicit filename and extension
  function triggerDownload(format) {
    if (!currentSessionId) return;
    const safeBase = (currentOriginalFilename || "provider_output").replace(/[^a-zA-Z0-9_\-]/g, '_');
    const filename = `${safeBase}_verified.${format}`;
    const downloadUrl = `/api/download/${encodeURIComponent(currentSessionId)}/${encodeURIComponent(filename)}?format=${encodeURIComponent(format)}`;

    const link = document.createElement('a');
    link.href = downloadUrl;
    link.setAttribute('download', filename);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }

  // Exports
  if (downloadXlsxBtn) {
    downloadXlsxBtn.addEventListener("click", () => triggerDownload("xlsx"));
  }
  if (downloadCsvBtn) {
    downloadCsvBtn.addEventListener("click", () => triggerDownload("csv"));
  }
  if (downloadJsonBtn) {
    downloadJsonBtn.addEventListener("click", () => triggerDownload("json"));
  }

  // Single Lookup Sandbox
  singleForm.addEventListener("submit", (e) => {
    e.preventDefault();

    const payload = {
      provider_name: document.getElementById("sp-name").value,
      firstname: document.getElementById("sp-firstname").value,
      lastname: document.getElementById("sp-lastname").value,
      organization: document.getElementById("sp-org").value,
      existing_npi: document.getElementById("sp-npi").value,
      city: document.getElementById("sp-city").value,
      state: document.getElementById("sp-state").value,
      phone: document.getElementById("sp-phone").value
    };

    spSubmitBtn.disabled = true;
    spSubmitBtn.innerHTML = "Searching...";

    fetch("/api/verify_single", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    })
      .then(res => res.json())
      .then(data => {
        spSubmitBtn.disabled = false;
        spSubmitBtn.innerHTML = "Verify Provider Now";
        renderSingleResult(data);
      })
      .catch(err => {
        spSubmitBtn.disabled = false;
        spSubmitBtn.innerHTML = "Verify Provider Now";
        alert("Error during verification: " + err);
      });
  });

  function renderSingleResult(data) {
    spResultContainer.classList.remove("hidden");
    const v = data.verification || {};
    const npiVal = data.npi_validation || {};

    let npiStatusBadge = npiVal.valid
      ? `<span class="badge badge-success">Valid ${npiVal.npi_type || 'NPI'}</span>`
      : `<span class="badge badge-danger">${npiVal.status || 'Invalid'}</span>`;

    if (!data.input.existing_npi) {
      npiStatusBadge = `<span class="badge badge-warning">Not Provided</span>`;
    }

    spResultContent.innerHTML = `
      <div class="res-box">
        <span class="res-label">Verification Status</span>
        <div class="res-val">${v.status === 'Verified' ? '<span class="badge badge-success">Verified</span>' : '<span class="badge badge-danger">Not Verified</span>'}</div>
      </div>
      <div class="res-box">
        <span class="res-label">Existing NPI Check</span>
        <div class="res-val">${npiStatusBadge}</div>
        <p style="font-size:0.78rem; color:var(--text-muted); margin-top:4px;">${npiVal.remarks || ''}</p>
      </div>
      <div class="res-box">
        <span class="res-label">Found NPI-1 Identifier</span>
        <div class="res-val">${v.npi !== 'Not Found' ? v.npi : 'Not Found'}</div>
        ${v.npi_url ? `<a href="${v.npi_url}" target="_blank" class="link-badge" style="margin-top:6px;">View Registry Profile</a>` : ''}
      </div>
      <div class="res-box">
        <span class="res-label">Verified Credential</span>
        <div class="res-val">${v.credential !== 'Unable to verify' ? `<span class="badge badge-success">${v.credential}</span>` : 'Unable to verify'}</div>
        ${v.source_url ? `<a href="${v.source_url}" target="_blank" class="link-badge" style="margin-top:6px;">View Source Web Page</a>` : ''}
      </div>
    `;
  }

  // Utilities
  function appendLog(type, text) {
    const line = document.createElement("div");
    line.className = `log-line ${type}`;
    const time = new Date().toLocaleTimeString();
    line.textContent = `[${time}] ${text}`;
    logConsole.appendChild(line);
    logConsole.scrollTop = logConsole.scrollHeight;
  }

  function escapeHtml(str) {
    if (!str) return "";
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
});
