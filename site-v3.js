import { renderInventory } from "./lib/inventory-view-v2.js";

const filterPanel = document.querySelector(".filters");
const mobileFilters = window.matchMedia("(max-width: 720px)");
function syncFilterPanel(event) {
  filterPanel.open = !event.matches;
}
syncFilterPanel(mobileFilters);
mobileFilters.addEventListener("change", syncFilterPanel);

const configResponse = await fetch("./data/vehicles.json", { cache: "no-store" });
if (!configResponse.ok) {
  throw new Error(`Vehicle configuration request failed with HTTP ${configResponse.status}`);
}

const siteConfig = await configResponse.json();
const requestedSlug = new URLSearchParams(window.location.search).get("vehicle");
const vehicleConfig = siteConfig.vehicles.find((vehicle) => vehicle.slug === requestedSlug)
  || siteConfig.vehicles.find((vehicle) => vehicle.slug === siteConfig.defaultVehicle)
  || siteConfig.vehicles[0];
if (!vehicleConfig) {
  throw new Error("No vehicles are configured.");
}

const vehicleSelector = document.querySelector("#vehicle-selector");
siteConfig.vehicles.forEach((vehicle) => {
  vehicleSelector.add(new Option(vehicle.displayName, vehicle.slug));
});
vehicleSelector.value = vehicleConfig.slug;
vehicleSelector.addEventListener("change", () => {
  const url = new URL(window.location.href);
  if (vehicleSelector.value === siteConfig.defaultVehicle) {
    url.searchParams.delete("vehicle");
  } else {
    url.searchParams.set("vehicle", vehicleSelector.value);
  }
  window.location.assign(url);
});

const dealerSearchLinks = vehicleConfig.dealerSearchLinks || [];
const dealerSearchNav = document.querySelector("#dealer-search-links");
const dealerSearchLinkList = document.querySelector("#dealer-search-link-list");
dealerSearchLinks.forEach((dealer) => {
  const link = document.createElement("a");
  link.href = dealer.url;
  link.textContent = dealer.name;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  dealerSearchLinkList.append(link);
});
dealerSearchNav.hidden = dealerSearchLinks.length === 0;

const dataRoot = `./vehicles/${vehicleConfig.slug}`;
const [inventoryResponse, historyResponse] = await Promise.all([
  fetch(`${dataRoot}/inventory.json`, { cache: "no-store" }),
  fetch(`${dataRoot}/inventory-history.json`, { cache: "no-store" })
]);
if (!inventoryResponse.ok) {
  throw new Error(`Static inventory request failed with HTTP ${inventoryResponse.status}`);
}

const history = historyResponse.ok
  ? await historyResponse.json()
  : { snapshots: [], vehicles: {} };
const vehicleName = vehicleConfig.vehicleName
  || `${vehicleConfig.make} ${vehicleConfig.model}`;
const intervalDays = vehicleConfig.refresh?.intervalDays;
const scheduleText = vehicleConfig.refresh?.enabled
  ? `Scheduled inventory snapshot every ${intervalDays === 1 ? "day" : `${intervalDays} days`}.`
  : "One-time inventory snapshot; automatic refresh is disabled.";

document.title = `${vehicleName} | ${siteConfig.siteTitle}`;
document.querySelector("#site-description").content =
  `Used ${vehicleName} listings in Washington from a static inventory snapshot.`;
document.querySelector("#brand-link").ariaLabel = `${siteConfig.siteTitle} home`;
document.querySelector("#brand-mark").textContent = "WA";
document.querySelector("#brand-name").textContent = siteConfig.siteTitle;
document.querySelector("#hero-title").textContent = `Washington ${vehicleName} listings`;
document.querySelector("#hero-description").textContent = scheduleText;
document.querySelector("#results-eyebrow").textContent =
  `USED ${vehicleName.toUpperCase()} · WASHINGTON`;
document.querySelector("#dealer-only-label").textContent = vehicleConfig.dealerOnlyLabel;
document.querySelector("#dealer-only-description").textContent =
  vehicleConfig.dealerOnlyDescription;

renderInventory(await inventoryResponse.json(), vehicleConfig, history);
