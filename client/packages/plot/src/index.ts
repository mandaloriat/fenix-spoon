export * from './scale.js';
export * from './curves.js';
export { type PlotCapabilities, PlotElement, definePlot } from './element.js';

import { definePlot } from './element.js';

// Importing the package registers the element, which is what a plain
// `<script type="module">` consumer expects. Import the `@fenix-spoon/plot/element`
// subpath instead to control registration (a different tag name, or none at all).
if (typeof customElements !== 'undefined') definePlot();

declare global {
  interface HTMLElementTagNameMap {
    'fs-plot': import('./element.js').PlotElement;
  }
}
