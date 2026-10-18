// Fit the interface to whatever screen it is being shown on.
//
// The UI is a fixed composition, authored at 1024x768: the arc geometry, the
// overlays and every inline SVG are written in those coordinates, and nothing
// under web/ reads the window size. On the device's own panel that is exactly
// the viewport, so the scale works out to 1 and this changes nothing at all.
//
// It matters everywhere else. A Google Cast receiver hands the page 960x540,
// which is 16:9 and so a third shorter in proportion than the design. Because
// html, body and .container all set overflow:hidden, the surplus is clipped
// rather than scrolled: the bottom of the arc simply is not there, with no
// scrollbar and no overflow for a script to notice. Measured on a BRAVIA, the
// document reported zero overflow in both axes while a sixth of the interface
// was missing.
//
// So letterbox rather than crop. Scale uniformly to fit and centre what is
// left over, which keeps the 4:3 composition intact — stretching an arc that
// was drawn around a circle would look worse than black bars.
(function () {
    'use strict';

    var DESIGN_W = 1024;
    var DESIGN_H = 768;

    function apply() {
        var scale = Math.min(window.innerWidth / DESIGN_W,
                             window.innerHeight / DESIGN_H);
        var style = document.documentElement.style;
        style.setProperty('--ui-scale', scale);
        style.setProperty('--ui-offset-x',
            ((window.innerWidth - DESIGN_W * scale) / 2) + 'px');
        style.setProperty('--ui-offset-y',
            ((window.innerHeight - DESIGN_H * scale) / 2) + 'px');
    }

    apply();
    window.addEventListener('resize', apply);
    // A Cast receiver can resize its viewport after the first paint.
    window.addEventListener('orientationchange', apply);
})();
