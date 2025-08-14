// Matrix-style cascading 0s and 1s background animation
(function() {
  "use strict";

  // Check for reduced motion preference
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    return; // Don't run animation if user prefers reduced motion
  }

  class MatrixRain {
    constructor(canvasId) {
      this.canvas = document.getElementById(canvasId);
      if (!this.canvas) return;

      this.ctx = this.canvas.getContext('2d');
      this.columns = [];
      this.fontSize = 14;
      this.resizeCanvas();
      this.initColumns();
      this.animate();

      // Handle window resize
      window.addEventListener('resize', () => this.resizeCanvas());
    }

    resizeCanvas() {
      this.canvas.width = window.innerWidth;
      this.canvas.height = window.innerHeight;
      this.columnCount = Math.floor(this.canvas.width / this.fontSize);
    }

    initColumns() {
      this.columns = [];
      for (let i = 0; i < this.columnCount; i++) {
        this.columns[i] = {
          x: i * this.fontSize,
          y: -Math.random() * this.canvas.height,
          speed: 1 + Math.random() * 3,
          chars: []
        };
      }
    }

    animate() {
      // Clear canvas with fade effect
      this.ctx.fillStyle = 'rgba(11, 24, 32, 0.05)';
      this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);

      this.ctx.fillStyle = '#00ff41';
      this.ctx.font = `${this.fontSize}px monospace`;

      for (let column of this.columns) {
        // Add new character at top
        if (Math.random() < 0.02) {
          column.chars.push({
            char: Math.random() > 0.5 ? '0' : '1',
            y: column.y,
            opacity: 1
          });
        }

        // Update and draw characters
        for (let i = column.chars.length - 1; i >= 0; i--) {
          const char = column.chars[i];
          char.y += column.speed;
          char.opacity -= 0.005;

          // Set opacity and draw
          this.ctx.globalAlpha = Math.max(0, char.opacity);
          this.ctx.fillText(char.char, column.x, char.y);

          // Remove if too faded or off screen
          if (char.opacity <= 0 || char.y > this.canvas.height) {
            column.chars.splice(i, 1);
          }
        }

        // Reset column position when it's far off screen
        if (column.y > this.canvas.height + 100 && column.chars.length === 0) {
          column.y = -Math.random() * this.canvas.height;
        } else {
          column.y += column.speed;
        }
      }

      this.ctx.globalAlpha = 1;
      requestAnimationFrame(() => this.animate());
    }
  }

  // Initialize when DOM is ready
  document.addEventListener('DOMContentLoaded', () => {
    new MatrixRain('matrixCanvas');
  });
})();