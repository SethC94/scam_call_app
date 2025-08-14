/**
 * Matrix-style falling 0s and 1s background animation
 * Creates a canvas-based animation of cascading binary digits
 */
(function() {
    'use strict';

    let canvas, ctx;
    let columns = [];
    let animationId;
    let isInitialized = false;

    // Configuration
    const CONFIG = {
        fontSize: 14,
        chars: ['0', '1'],
        speed: 0.5,
        opacity: 0.8,
        color: '#00ff00', // Matrix green
        trailLength: 0.05
    };

    // Column class to manage each falling stream
    class MatrixColumn {
        constructor(x, canvasHeight) {
            this.x = x;
            this.y = Math.random() * canvasHeight;
            this.speed = CONFIG.speed + Math.random() * 0.5;
            this.chars = [];
            this.maxLength = Math.floor(Math.random() * 20) + 10;
            this.generateChars();
        }

        generateChars() {
            this.chars = [];
            for (let i = 0; i < this.maxLength; i++) {
                this.chars.push(CONFIG.chars[Math.floor(Math.random() * CONFIG.chars.length)]);
            }
        }

        update(canvasHeight) {
            this.y += this.speed;
            
            // Reset when column goes off screen
            if (this.y > canvasHeight + this.maxLength * CONFIG.fontSize) {
                this.y = -this.maxLength * CONFIG.fontSize;
                this.speed = CONFIG.speed + Math.random() * 0.5;
                this.maxLength = Math.floor(Math.random() * 20) + 10;
                this.generateChars();
            }
        }

        draw(ctx) {
            ctx.font = `${CONFIG.fontSize}px monospace`;
            
            for (let i = 0; i < this.chars.length; i++) {
                const charY = this.y + i * CONFIG.fontSize;
                const alpha = Math.max(0, 1 - (i / this.chars.length)) * CONFIG.opacity;
                
                // Brighter green for the leading character
                if (i === 0) {
                    ctx.fillStyle = `rgba(255, 255, 255, ${alpha})`;
                } else {
                    ctx.fillStyle = `rgba(0, 255, 0, ${alpha})`;
                }
                
                ctx.fillText(this.chars[i], this.x, charY);
            }
        }
    }

    function initCanvas() {
        if (isInitialized) return;

        canvas = document.createElement('canvas');
        canvas.id = 'matrixCanvas';
        ctx = canvas.getContext('2d');
        
        // Insert canvas at the beginning of body
        document.body.insertBefore(canvas, document.body.firstChild);
        
        resizeCanvas();
        initColumns();
        isInitialized = true;
    }

    function resizeCanvas() {
        if (!canvas) return;
        
        canvas.width = window.innerWidth;
        canvas.height = window.innerHeight;
        
        // Reinitialize columns on resize
        if (isInitialized) {
            initColumns();
        }
    }

    function initColumns() {
        if (!canvas) return;
        
        columns = [];
        const columnWidth = CONFIG.fontSize;
        const numColumns = Math.floor(canvas.width / columnWidth);
        
        for (let i = 0; i < numColumns; i++) {
            const x = i * columnWidth;
            columns.push(new MatrixColumn(x, canvas.height));
        }
    }

    function animate() {
        if (!canvas || !ctx) return;
        
        // Clear canvas with slight trail effect
        ctx.fillStyle = `rgba(11, 24, 32, ${CONFIG.trailLength})`;
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        
        // Update and draw columns
        columns.forEach(column => {
            column.update(canvas.height);
            column.draw(ctx);
        });
        
        animationId = requestAnimationFrame(animate);
    }

    function startAnimation() {
        if (!isInitialized) {
            initCanvas();
        }
        
        if (!animationId) {
            animate();
        }
    }

    function stopAnimation() {
        if (animationId) {
            cancelAnimationFrame(animationId);
            animationId = null;
        }
    }

    // Initialize when DOM is ready
    function init() {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', startAnimation);
        } else {
            startAnimation();
        }
        
        // Handle window resize
        window.addEventListener('resize', resizeCanvas);
        
        // Handle visibility change to pause/resume animation
        document.addEventListener('visibilitychange', function() {
            if (document.hidden) {
                stopAnimation();
            } else {
                startAnimation();
            }
        });
    }

    // Auto-initialize
    init();

    // Export functions for manual control if needed
    window.MatrixAnimation = {
        start: startAnimation,
        stop: stopAnimation,
        isRunning: () => !!animationId
    };
})();