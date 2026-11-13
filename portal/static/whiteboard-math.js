/* MathJax is vendored locally; untrusted TeX cannot load packages or HTML. */
window.MathJax = {
  startup: {typeset: false},
  tex: {packages: ['base', 'ams', 'newcommand'], maxBuffer: 10000, maxMacros: 1000},
  svg: {fontCache: 'local'},
  options: {enableMenu: false, enableAssistiveMml: false}
};
