(function() {
  'use strict';

  class MatrixEffect {
    constructor() {
      this.canvas = null;
      this.ctx = null;
      this.drops = [];
      this.characters = '01';
      this.fontSize = 14;
      this.animationId = null;
      this.isInitialized = false;
    }

    init() {
      if (this.isInitialized) return;

      // Create canvas element
      this.canvas = document.createElement('canvas');
      this.canvas.id = 'matrix-canvas';
      this.canvas.style.cssText = `
        position: fixed;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        z-index: -1;
        opacity: 0.15;
        pointer-events: none;
      `;

      // Insert canvas as first child of body
      document.body.insertBefore(this.canvas, document.body.firstChild);

      this.ctx = this.canvas.getContext('2d');
      this.setupCanvas();
      this.initDrops();
      this.start();

      // Handle window resize
      window.addEventListener('resize', () => this.handleResize());

      this.isInitialized = true;
    }

    setupCanvas() {
      const dpr = window.devicePixelRatio || 1;
      const rect = this.canvas.getBoundingClientRect();
      
      this.canvas.width = rect.width * dpr;
      this.canvas.height = rect.height * dpr;
      
      this.ctx.scale(dpr, dpr);
      this.canvas.style.width = rect.width + 'px';
      this.canvas.style.height = rect.height + 'px';

      // Set canvas style
      this.ctx.fillStyle = '#000';
      this.ctx.fillRect(0, 0, rect.width, rect.height);
      this.ctx.font = `${this.fontSize}px monospace`;
    }

    initDrops() {
      const rect = this.canvas.getBoundingClientRect();
      const columns = Math.floor(rect.width / this.fontSize);
      
      this.drops = [];
      for (let i = 0; i < columns; i++) {
        this.drops[i] = Math.random() * rect.height;
      }
    }

    handleResize() {
      this.setupCanvas();
      this.initDrops();
    }

    animate() {
      const rect = this.canvas.getBoundingClientRect();
      
      // Create trailing effect
      this.ctx.fillStyle = 'rgba(0, 0, 0, 0.05)';
      this.ctx.fillRect(0, 0, rect.width, rect.height);

      // Matrix green color
      this.ctx.fillStyle = '#0f0';

      // Draw characters
      for (let i = 0; i < this.drops.length; i++) {
        const char = this.characters[Math.floor(Math.random() * this.characters.length)];
        const x = i * this.fontSize;
        const y = this.drops[i];

        this.ctx.fillText(char, x, y);

        // Reset drop if it goes off screen or randomly
        if (y > rect.height && Math.random() > 0.975) {
          this.drops[i] = 0;
        }

        this.drops[i] += this.fontSize;
      }

      this.animationId = requestAnimationFrame(() => this.animate());
    }

    start() {
      if (!this.animationId) {
        this.animate();
      }
    }

    stop() {
      if (this.animationId) {
        cancelAnimationFrame(this.animationId);
        this.animationId = null;
      }
    }

    destroy() {
      this.stop();
      if (this.canvas && this.canvas.parentNode) {
        this.canvas.parentNode.removeChild(this.canvas);
      }
      this.isInitialized = false;
    }
  }

  // Auto-initialize when DOM is ready
  function initMatrix() {
    const matrix = new MatrixEffect();
    matrix.init();
    
    // Store reference for potential cleanup
    window.matrixEffect = matrix;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initMatrix);
  } else {
    initMatrix();
  }
})();