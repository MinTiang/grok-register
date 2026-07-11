function getRandomInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
}

// Patch MouseEvent to return randomized screen coordinates per event.
// The old approach set static values once, which Cloudflare can detect
// because real users generate different coordinates for every mouse event.

Object.defineProperty(MouseEvent.prototype, 'screenX', {
    get: function () {
        return getRandomInt(800, 1200);
    },
});

Object.defineProperty(MouseEvent.prototype, 'screenY', {
    get: function () {
        return getRandomInt(400, 600);
    },
});
