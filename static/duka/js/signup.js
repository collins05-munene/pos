(() => {
  const form = document.getElementById('signup-form');
  const alertBox = document.getElementById('alert');
  const plansEl = document.getElementById('plans');
  const planNote = document.getElementById('plan-note');
  const submitBtn = document.getElementById('submit-btn');
  let selectedTerm = 1;
  let plans = [];
  let branchCount = 1;
  let branchInfo = { max: 10, extra_monthly: 0 };
  const branchSelect = document.getElementById('branch_count');
  const branchNote = document.getElementById('branch-note');
  const branchNamesEl = document.getElementById('branch-names');

  function showAlert(msg) {
    alertBox.textContent = msg;
    alertBox.classList.add('show');
    alertBox.scrollIntoView({ block: 'nearest' });
  }
  function clearAlert() { alertBox.classList.remove('show'); alertBox.textContent = ''; }

  function clearFieldErrors() {
    form.querySelectorAll('.field').forEach(f => {
      f.classList.remove('invalid');
      const e = f.querySelector('.err');
      if (e) e.textContent = '';
    });
  }
  function applyFieldErrors(errors) {
    Object.entries(errors || {}).forEach(([name, msgs]) => {
      const field = form.querySelector(`[data-field="${name}"]`);
      if (!field) return;
      field.classList.add('invalid');
      const list = Array.isArray(msgs) ? msgs : [msgs];
      field.querySelector('.err').textContent = list.map(m => (m && m.message) || m).join(' ');
    });
  }

  function renderPlans() {
    plansEl.innerHTML = '';
    plans.forEach(p => {
      const btn = Duka.el('button', {
        type: 'button', class: 'plan', 'aria-pressed': String(p.term_months === selectedTerm),
        onclick: () => { selectedTerm = p.term_months; renderPlans(); updateNote(); },
      }, [
        Duka.el('span', { class: 'months', text: `${p.term_months} mo` }),
        Duka.el('span', { class: 'price', text: Duka.money(p.subscription_amount) }),
        Number(p.discount_percent) > 0 ? Duka.el('span', { class: 'save', text: `save ${p.discount_percent}%` }) : null,
      ]);
      plansEl.appendChild(btn);
    });
  }

  function branchNames() {
    return Array.from(branchNamesEl.querySelectorAll('input')).map(i => i.value.trim());
  }

  function renderBranchInputs() {
    const old = branchNames();
    branchNamesEl.innerHTML = '';
    for (let i = 0; i < branchCount; i++) {
      const input = Duka.el('input', {
        type: 'text', maxlength: '100',
        placeholder: i === 0 ? 'Main Branch' : `Branch ${i + 1} name`,
        value: old[i] || '',
      });
      branchNamesEl.appendChild(Duka.el('div', { class: 'field' }, [
        Duka.el('label', { text: i === 0 ? 'Branch 1 (main)' : `Branch ${i + 1}` }), input,
      ]));
    }
    branchNote.textContent = branchCount === 1
      ? 'One branch is included. You can add more any time from Branches.'
      : `Branch 1 is included. ${branchCount - 1} extra ${branchCount === 2 ? 'branch costs' : 'branches cost'} ` +
        `${Duka.money(Number(branchInfo.extra_monthly) * (branchCount - 1))}/month in total ` +
        `(${Duka.money(branchInfo.extra_monthly)} each, before any prepay discount).`;
  }

  function initBranchSelect() {
    branchSelect.innerHTML = '';
    for (let n = 1; n <= Number(branchInfo.max); n++) {
      branchSelect.appendChild(Duka.el('option', { value: String(n), text: n === 1 ? '1 branch' : `${n} branches` }));
    }
    branchSelect.value = String(branchCount);
    branchSelect.addEventListener('change', () => {
      branchCount = Number(branchSelect.value);
      renderBranchInputs();
      loadPricing();
    });
  }

  function updateNote() {
    const p = plans.find(x => x.term_months === selectedTerm);
    if (!p) return;
    planNote.textContent = `Total due today: ${Duka.money(p.total)} -- ` +
      `${Duka.money(p.subscription_amount)} subscription + ${Duka.money(p.install_fee)} one-time installation. ` +
      `Works out to ${Duka.money(p.effective_monthly)}/month.`;
  }

  async function loadPricing() {
    try {
      const data = await Duka.api(`.?branches=${branchCount}`);
      plans = data.pricing;
      if (data.branches && !branchSelect.options.length) {
        branchInfo = data.branches;
        initBranchSelect();
        renderBranchInputs();
      }
      renderPlans();
      updateNote();
    } catch (e) {
      planNote.textContent = 'Could not load pricing -- refresh the page.';
    }
  }

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    clearAlert();
    clearFieldErrors();
    submitBtn.disabled = true;
    submitBtn.textContent = 'Creating your account\u2026';

    const payload = {
      business_name: form.business_name.value.trim(),
      business_type: form.business_type.value,
      owner_name: form.owner_name.value.trim(),
      email: form.email.value.trim(),
      phone: form.phone.value.trim(),
      password: form.password.value,
      term_months: selectedTerm,
      branch_count: branchCount,
      branch_names: branchNames(),
    };

    try {
      const data = await Duka.api('.', { method: 'POST', body: payload });
      window.location.href = data.next || '/billing/';
    } catch (e) {
      if (e.data && e.data.errors) {
        applyFieldErrors(e.data.errors);
        if (e.data.errors.__all__) showAlert(e.data.errors.__all__.map(m => m.message || m).join(' '));
        else showAlert('Please fix the highlighted fields.');
      } else {
        showAlert(e.message);
      }
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = "Create my shop's account \u2192";
    }
  });

  loadPricing();
})();
