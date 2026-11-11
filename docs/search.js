const query = document.querySelector("#query");
const count = document.querySelector("#count");
const articles = [...document.querySelectorAll("article[data-search]")];

function filterArticles() {
  const terms = query.value.toLowerCase().trim().split(/\s+/).filter(Boolean);
  let visible = 0;
  for (const article of articles) {
    article.hidden = !terms.every(term => article.dataset.search.includes(term));
    if (!article.hidden) visible += 1;
  }
  count.textContent = visible === 1 ? "1 guide" : `${visible} guides`;
}

query.addEventListener("input", filterArticles);
filterArticles();

function openLinkedGuide() {
  const guide = articles.find(article => location.hash === `#${article.id}`);
  if (!guide) return;
  if (guide.hidden) {
    query.value = "";
    filterArticles();
  }
  guide.querySelector("details").open = true;
}

for (const guide of articles) {
  guide.querySelector("h3 a").addEventListener("click", () => {
    guide.querySelector("details").open = true;
  });
}

window.addEventListener("hashchange", openLinkedGuide);
openLinkedGuide();
